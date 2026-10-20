from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from forwarded_support import HOST, ISSUER, TYP, Proxy
from forwarded_support import config as forwarded_config
from test_auth_builtin import (  # noqa: F401
    BOTH_SCOPES,
    PASSWORD,
    REDIRECT,
    client,
    config,
    owner,
    register,
    start_login,
    token_for,
)
from test_auth_oidc import Idp
from test_server import _collect, _Unreached

from notes_vault_mcp.auth import AuthConfig
from notes_vault_mcp.auth.builtin import BuiltinProvider
from notes_vault_mcp.auth.forwarded import ForwardedIdentity, ForwardedVerifier, IdentityError
from notes_vault_mcp.auth.http import BearerToken

# --- bearer ---------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_non_ascii_authorization_header_is_refused_with_401():
    inner = _Unreached()
    status = await _collect(BearerToken(inner, "s3cret"), [(b"authorization", b"Bearer s\xffcret")])
    assert status == 401 and inner.called is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    [
        b"bearer s3cret",
        b"Bearer  s3cret",
        b"Bearer s3cret ",
        b"Bearer s3cre",
        b"Bearer s3cret2",
        b"Basic czNjcmV0",
        b"s3cret",
    ],
)
async def test_only_the_exact_bearer_header_is_accepted(header: bytes):
    inner = _Unreached()
    assert await _collect(BearerToken(inner, "s3cret"), [(b"authorization", header)]) == 401
    assert inner.called is False


@pytest.mark.anyio
async def test_the_bearer_guard_reads_the_header_case_insensitively():
    inner = _Unreached()
    assert await _collect(BearerToken(inner, "s3cret"), [(b"AUTHORIZATION", b"Bearer s3cret")]) == 200


@pytest.mark.anyio
async def test_the_bearer_guard_lets_the_lifespan_through_but_nothing_else_non_http():
    seen: list[str] = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    guard = BearerToken(inner, "s3cret")
    await guard({"type": "lifespan"}, None, None)
    await guard({"type": "websocket", "headers": []}, None, None)
    assert seen == ["lifespan", "websocket"]


# --- forwarded identity ---------------------------------------------------------------------------------------


def _claims(proxy: Proxy, **overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": HOST, "sub": "7", "entitlements": ["vault:write"], "exp": now + 60, "typ": TYP}
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def _refused(proxy: Proxy, tmp_path: Path, token: str) -> str:
    with pytest.raises(IdentityError) as caught:
        ForwardedVerifier(forwarded_config(proxy, tmp_path)).verify(token)
    return caught.value.error


def test_an_unsigned_token_is_refused(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy), key=None, algorithm="none")
    assert _refused(proxy, tmp_path, token) == "invalid_token"


def test_a_token_hmac_signed_with_the_public_key_is_refused(proxy: Proxy, tmp_path: Path):
    try:
        token = jwt.encode(_claims(proxy), key=proxy.pem.encode(), algorithm="HS256")
    except jwt.exceptions.InvalidKeyError:
        token = jwt.encode(_claims(proxy), key="x" * 64, algorithm="HS256")
    assert _refused(proxy, tmp_path, token) == "invalid_token"


@pytest.mark.parametrize("missing", ["sub", "aud", "exp", "iss"])
def test_a_token_missing_a_required_claim_is_refused(proxy: Proxy, tmp_path: Path, missing: str):
    token = jwt.encode(_claims(proxy, **{missing: None}), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token) == "invalid_token"


def test_a_token_for_another_host_is_refused(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, aud="other.example.com"), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token) == "invalid_token"


def test_an_expired_or_not_yet_valid_token_is_refused(proxy: Proxy, tmp_path: Path):
    now = int(time.time())
    expired = jwt.encode(_claims(proxy, exp=now - 1), proxy.key, algorithm="RS256")
    early = jwt.encode(_claims(proxy, nbf=now + 300), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, expired) == "invalid_token"
    assert _refused(proxy, tmp_path, early) == "invalid_token"


def test_a_token_of_another_type_is_refused_when_a_type_is_configured(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, typ="access"), proxy.key, algorithm="RS256")
    assert _refused(proxy, tmp_path, token) == "invalid_token"


def test_entitlements_may_arrive_as_one_string(proxy: Proxy, tmp_path: Path):
    token = jwt.encode(_claims(proxy, entitlements="vault:read vault:write"), proxy.key, algorithm="RS256")
    assert ForwardedVerifier(forwarded_config(proxy, tmp_path)).verify(token).scopes == ["vault:read", "vault:write"]


def test_garbage_in_the_identity_header_is_refused(proxy: Proxy, tmp_path: Path):
    for token in ("", "not.a.jwt", "a.b.c", "\xff\xfe", "Bearer x"):
        with pytest.raises(IdentityError):
            ForwardedVerifier(forwarded_config(proxy, tmp_path)).verify(token)


async def _through(middleware: ForwardedIdentity, path: str, headers: list[tuple[bytes, bytes]]) -> int:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await middleware({"type": "http", "path": path, "headers": headers, "method": "GET"}, receive, send)
    return sent[0]["status"]


@pytest.fixture
def guard(proxy: Proxy, tmp_path: Path) -> tuple[ForwardedIdentity, _Unreached]:
    inner = _Unreached()
    verifier = ForwardedVerifier(forwarded_config(proxy, tmp_path))
    return ForwardedIdentity(inner, verifier, "X-Forwarded-Identity", health_path="/up", open_prefixes=("/dav",)), inner


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/up/", "/upx", "/UP", "/dav-x", "/davx/y", "//dav/x", "/mcp"])
async def test_only_the_exact_health_path_and_open_prefix_skip_the_identity(guard, path: str):
    middleware, inner = guard
    assert await _through(middleware, path, []) == 403
    assert inner.called is False


@pytest.mark.anyio
async def test_the_identity_header_name_is_matched_case_insensitively(guard, proxy: Proxy):
    middleware, inner = guard
    assert await _through(middleware, "/mcp", [(b"x-forwarded-identity", proxy.token().encode())]) == 200
    assert inner.called is True


@pytest.mark.anyio
async def test_a_bearer_token_never_stands_in_for_the_forwarded_identity(guard, proxy: Proxy):
    middleware, inner = guard
    assert await _through(middleware, "/mcp", [(b"authorization", b"Bearer " + proxy.token().encode())]) == 403
    assert inner.called is False


# --- builtin login --------------------------------------------------------------------------------------------


@pytest.fixture
async def http(client: httpx2.AsyncClient):  # noqa: F811
    yield client


@pytest.mark.anyio
async def test_the_login_page_cannot_be_framed_or_cached(http: httpx2.AsyncClient):
    registration = await register(http)
    _, request_id = await start_login(http, registration)
    response = await http.get("/login", params={"request": request_id})
    csp = response.headers.get("content-security-policy", "")
    assert response.headers.get("x-frame-options", "").upper() == "DENY" or "frame-ancestors" in csp
    assert "no-store" in response.headers.get("cache-control", "")


@pytest.mark.anyio
async def test_a_wrong_password_is_never_echoed(http: httpx2.AsyncClient):
    registration = await register(http)
    _, request_id = await start_login(http, registration)
    response = await http.post("/login", data={"request": request_id, "password": "my-typo-pw", "read": "1"})
    assert response.status_code == 401
    assert "my-typo-pw" not in response.text


@pytest.mark.anyio
async def test_a_client_name_is_escaped_on_the_login_page(http: httpx2.AsyncClient):
    registration = await register(http, name='<img src=x onerror="alert(1)">')
    _, request_id = await start_login(http, registration)
    page = (await http.get("/login", params={"request": request_id})).text
    assert "<img" not in page and "&lt;img" in page


@pytest.mark.anyio
async def test_a_refresh_may_not_widen_the_granted_scopes(http: httpx2.AsyncClient):
    registration = await register(http)
    tokens = await token_for(http, registration, scope="vault:read")
    response = await http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
            "scope": BOTH_SCOPES,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_scope"


@pytest.mark.anyio
async def test_a_code_minted_for_one_client_cannot_be_exchanged_by_another(http: httpx2.AsyncClient):
    first = await register(http, name="first")
    second = await register(http, name="second")
    verifier, request_id = await start_login(http, first)
    redirect = await http.post("/login", data={"request": request_id, "password": PASSWORD, "read": "1"})
    assert redirect.status_code == 302
    code = httpx2.URL(redirect.headers["location"]).params["code"]
    response = await http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT,
            "client_id": second["client_id"],
            "client_secret": second["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_a_login_request_is_consumed_by_one_approval(http: httpx2.AsyncClient):
    registration = await register(http)
    _, request_id = await start_login(http, registration)
    first = await http.post("/login", data={"request": request_id, "password": PASSWORD, "read": "1"})
    again = await http.post("/login", data={"request": request_id, "password": PASSWORD, "read": "1"})
    assert first.status_code == 302
    assert again.status_code == 400 and "expired" in again.text


def test_the_lockout_guards_the_one_owner_password_and_forgets_after_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    provider = BuiltinProvider(AuthConfig(mode="builtin", public_url="http://localhost", auth_dir=tmp_path))
    moment = [1000.0]
    monkeypatch.setattr("notes_vault_mcp.auth.builtin.time.monotonic", lambda: moment[0])
    for _ in range(50):
        provider.record_failure()
    assert provider.locked_out() is True
    assert len(provider.failures) <= 5
    moment[0] += 61
    assert provider.locked_out() is False


# --- oidc -----------------------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_unsigned_oidc_token_is_refused(idp: Idp):
    payload = {"iss": idp.issuer, "sub": "ida", "exp": int(time.time()) + 300, "groups": ["vault-writers"]}
    token = jwt.encode(payload, key=None, algorithm="none", headers={"kid": "one"})
    assert await idp.verifier().verify_token(token) is None


@pytest.mark.anyio
async def test_an_hmac_signed_oidc_token_is_refused(idp: Idp):
    payload = {"iss": idp.issuer, "sub": "ida", "exp": int(time.time()) + 300, "groups": ["vault-writers"]}
    token = jwt.encode(payload, key="x" * 64, algorithm="HS256", headers={"kid": "one"})
    assert await idp.verifier().verify_token(token) is None


@pytest.mark.anyio
async def test_a_token_with_a_padded_signature_is_refused(idp: Idp):
    token = idp.token(groups=["vault-writers"])
    assert await idp.verifier().verify_token(token + "!!!!") is None


@pytest.fixture
def idp() -> Idp:
    return Idp()
