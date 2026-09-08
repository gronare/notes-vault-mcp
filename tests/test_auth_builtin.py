from __future__ import annotations

import base64
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

from notes_vault_mcp.auth import AuthConfig
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.cli import main
from notes_vault_mcp.vault import Vault

BASE = "http://localhost:8765"
PASSWORD = "correct horse battery staple"
REDIRECT = "http://localhost:9876/callback"
BOTH_SCOPES = "vault:read vault:write"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "builtin-auth-test", "version": "1"},
    },
}


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuthConfig:
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path))
    return AuthConfig(mode="builtin", public_url="http://localhost", issuer="http://localhost", auth_dir=tmp_path)


@pytest.fixture
def owner(config: AuthConfig) -> AuthConfig:
    assert main(["owner", "set-password", "--password", PASSWORD]) == 0
    return config


@asynccontextmanager
async def serving(vault: Vault, config: AuthConfig):
    app = build_http_app(vault, config)
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
            yield http


@pytest.fixture
async def client(vault: Vault, owner: AuthConfig):
    async with serving(vault, owner) as http:
        yield http


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def query_of(response: httpx2.Response, key: str) -> str:
    return parse_qs(urlsplit(response.headers["location"]).query)[key][0]


async def register(client: httpx2.AsyncClient, name: str = "Test client") -> dict[str, Any]:
    response = await client.post(
        "/register",
        json={
            "client_name": name,
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
            "scope": BOTH_SCOPES,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def start_login(
    client: httpx2.AsyncClient, registration: dict[str, Any], scope: str = BOTH_SCOPES
) -> tuple[str, str]:
    verifier, challenge = pkce_pair()
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "state-42",
            "scope": scope,
            "resource": "http://localhost/mcp",
        },
    )
    assert response.status_code == 302, response.text
    return verifier, query_of(response, "request")


async def approve(
    client: httpx2.AsyncClient, request_id: str, write: bool = True, password: str = PASSWORD
) -> httpx2.Response:
    form = {"request": request_id, "password": password, "read": "1"}
    if write:
        form["write"] = "1"
    return await client.post("/login", data=form)


async def token_for(
    client: httpx2.AsyncClient, registration: dict[str, Any], scope: str = BOTH_SCOPES
) -> dict[str, Any]:
    verifier, request_id = await start_login(client, registration, scope)
    redirect = await approve(client, request_id, write="vault:write" in scope)
    assert redirect.status_code == 302, redirect.text
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query_of(redirect, "code"),
            "redirect_uri": REDIRECT,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def sse_payload(text: str) -> dict[str, Any]:
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    raise AssertionError(f"no data frame in {text!r}")


async def post_mcp(
    client: httpx2.AsyncClient, token: str, body: dict[str, Any], session: str | None = None
) -> httpx2.Response:
    headers = dict(MCP_HEADERS, Authorization=f"Bearer {token}")
    if session is not None:
        headers["mcp-session-id"] = session
    return await client.post("/mcp", headers=headers, content=json.dumps(body))


async def open_session(client: httpx2.AsyncClient, token: str) -> str:
    response = await post_mcp(client, token, INITIALIZE)
    assert response.status_code == 200, response.text
    session = response.headers["mcp-session-id"]
    ready = await post_mcp(client, token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    assert ready.status_code == 202, ready.text
    return session


@pytest.mark.anyio
async def test_the_authorization_server_metadata_names_every_endpoint(client: httpx2.AsyncClient):
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    assert metadata["issuer"] == "http://localhost"
    assert metadata["authorization_endpoint"] == "http://localhost/authorize"
    assert metadata["token_endpoint"] == "http://localhost/token"
    assert metadata["registration_endpoint"] == "http://localhost/register"
    assert metadata["revocation_endpoint"] == "http://localhost/revoke"
    assert metadata["code_challenge_methods_supported"] == ["S256"]
    assert set(metadata["scopes_supported"]) == {"vault:read", "vault:write"}


@pytest.mark.anyio
async def test_the_protected_resource_metadata_points_at_this_server(client: httpx2.AsyncClient):
    metadata = (await client.get("/.well-known/oauth-protected-resource/mcp")).json()
    assert metadata["resource"] == "http://localhost/mcp"
    assert metadata["authorization_servers"] == ["http://localhost"]
    assert metadata["scopes_supported"] == ["vault:read"]


@pytest.mark.anyio
async def test_a_client_registers_itself_and_is_remembered(client: httpx2.AsyncClient):
    registration = await register(client, name="Claude Code")
    assert registration["client_id"]
    assert registration["client_secret"]
    assert registration["client_name"] == "Claude Code"
    again = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": pkce_pair()[1],
            "code_challenge_method": "S256",
        },
    )
    assert again.status_code == 302


@pytest.mark.anyio
async def test_authorize_sends_the_browser_to_the_login_page(client: httpx2.AsyncClient):
    registration = await register(client)
    verifier, request_id = await start_login(client, registration)
    assert verifier
    response = await client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": REDIRECT,
            "code_challenge": pkce_pair()[1],
            "code_challenge_method": "S256",
            "state": "state-42",
        },
    )
    assert response.headers["location"].startswith("http://localhost/login?request=")
    assert request_id not in response.headers["location"]


@pytest.mark.anyio
async def test_the_login_page_offers_read_ticked_and_write_unticked(client: httpx2.AsyncClient):
    registration = await register(client, name="Claude Code")
    _, request_id = await start_login(client, registration)
    page = (await client.get("/login", params={"request": request_id})).text
    assert 'name="read" value="1" checked' in page
    assert 'name="write" value="1">' in page
    assert f'name="request" value="{request_id}"' in page
    assert 'type="password"' in page
    assert "Claude Code" in page
    assert "<script src=" not in page
    assert "<link" not in page


@pytest.mark.anyio
async def test_an_unknown_login_request_has_nothing_to_approve(client: httpx2.AsyncClient):
    response = await client.get("/login", params={"request": "made-up"})
    assert response.status_code == 400
    assert "expired" in response.text
    assert "<form" not in response.text


@pytest.mark.anyio
async def test_a_server_without_an_owner_password_says_so_and_lets_nobody_in(vault: Vault, config: AuthConfig):
    async with serving(vault, config) as client:
        registration = await register(client)
        _, request_id = await start_login(client, registration)
        page = await client.get("/login", params={"request": request_id})
        assert "owner set-password" in page.text
        refused = await approve(client, request_id)
        assert refused.status_code == 401


@pytest.mark.anyio
async def test_a_wrong_password_is_refused_and_the_form_comes_back(client: httpx2.AsyncClient):
    registration = await register(client)
    _, request_id = await start_login(client, registration)
    response = await approve(client, request_id, password="not the password")
    assert response.status_code == 401
    assert "Wrong password" in response.text
    assert f'name="request" value="{request_id}"' in response.text


@pytest.mark.anyio
async def test_five_wrong_passwords_lock_the_caller_out(client: httpx2.AsyncClient):
    registration = await register(client)
    _, request_id = await start_login(client, registration)
    for _ in range(5):
        assert (await approve(client, request_id, password="wrong")).status_code == 401
    locked = await approve(client, request_id)
    assert locked.status_code == 429
    assert "Too many failed attempts" in locked.text


@pytest.mark.anyio
async def test_approving_read_only_grants_only_the_read_scope(client: httpx2.AsyncClient):
    registration = await register(client)
    verifier, request_id = await start_login(client, registration)
    redirect = await approve(client, request_id, write=False)
    assert redirect.status_code == 302
    assert query_of(redirect, "state") == "state-42"
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query_of(redirect, "code"),
            "redirect_uri": REDIRECT,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    )
    payload = response.json()
    assert payload["scope"] == "vault:read"
    assert payload["expires_in"] == 3600
    assert payload["refresh_token"]


@pytest.mark.anyio
async def test_the_token_exchange_refuses_a_wrong_pkce_verifier(client: httpx2.AsyncClient):
    registration = await register(client)
    _, request_id = await start_login(client, registration)
    redirect = await approve(client, request_id)
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query_of(redirect, "code"),
            "redirect_uri": REDIRECT,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": secrets.token_urlsafe(48),
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


@pytest.mark.anyio
async def test_a_code_may_be_exchanged_only_once(client: httpx2.AsyncClient):
    registration = await register(client)
    verifier, request_id = await start_login(client, registration)
    redirect = await approve(client, request_id)
    form = {
        "grant_type": "authorization_code",
        "code": query_of(redirect, "code"),
        "redirect_uri": REDIRECT,
        "client_id": registration["client_id"],
        "client_secret": registration["client_secret"],
        "code_verifier": verifier,
    }
    assert (await client.post("/token", data=form)).status_code == 200
    replayed = await client.post("/token", data=form)
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"


@pytest.mark.anyio
async def test_an_expired_code_is_refused(client: httpx2.AsyncClient, monkeypatch: pytest.MonkeyPatch):
    from notes_vault_mcp.auth import builtin

    registration = await register(client)
    verifier, request_id = await start_login(client, registration)
    monkeypatch.setattr(builtin, "CODE_TTL", -1)
    redirect = await approve(client, request_id)
    response = await client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": query_of(redirect, "code"),
            "redirect_uri": REDIRECT,
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
            "code_verifier": verifier,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert "expired" in response.json()["error_description"]


@pytest.mark.anyio
async def test_mcp_refuses_a_request_without_a_token(client: httpx2.AsyncClient):
    response = await client.post("/mcp", headers=MCP_HEADERS, content=json.dumps(INITIALIZE))
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"' in challenge


@pytest.mark.anyio
async def test_mcp_accepts_a_token_from_the_login_flow(client: httpx2.AsyncClient):
    registration = await register(client)
    token = (await token_for(client, registration))["access_token"]
    session = await open_session(client, token)
    listing = await post_mcp(client, token, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    assert listing.status_code == 200
    names = {tool["name"] for tool in sse_payload(listing.text)["result"]["tools"]}
    assert {"search", "read_file", "write_file"} <= names


@pytest.mark.anyio
async def test_a_read_only_token_may_not_write(client: httpx2.AsyncClient):
    registration = await register(client)
    token = (await token_for(client, registration, scope="vault:read"))["access_token"]
    session = await open_session(client, token)
    call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "write_file", "arguments": {"path": "Areas/probe.md", "content": "rad"}},
    }
    response = await post_mcp(client, token, call, session)
    assert response.status_code == 200
    result = sse_payload(response.text)["result"]
    assert result["isError"] is True
    assert "vault:write" in result["content"][0]["text"]


@pytest.mark.anyio
async def test_refreshing_rotates_both_tokens(client: httpx2.AsyncClient):
    registration = await register(client)
    first = await token_for(client, registration)
    form = {
        "grant_type": "refresh_token",
        "refresh_token": first["refresh_token"],
        "client_id": registration["client_id"],
        "client_secret": registration["client_secret"],
    }
    response = await client.post("/token", data=form)
    assert response.status_code == 200, response.text
    second = response.json()
    assert second["access_token"] != first["access_token"]
    assert second["refresh_token"] != first["refresh_token"]
    replayed = await client.post("/token", data=form)
    assert replayed.status_code == 400
    assert replayed.json()["error"] == "invalid_grant"
    await open_session(client, second["access_token"])


@pytest.mark.anyio
async def test_a_revoked_token_stops_reaching_the_vault(client: httpx2.AsyncClient):
    registration = await register(client)
    token = await token_for(client, registration)
    await open_session(client, token["access_token"])
    revoked = await client.post(
        "/revoke",
        data={
            "token": token["access_token"],
            "client_id": registration["client_id"],
            "client_secret": registration["client_secret"],
        },
    )
    assert revoked.status_code == 200
    refused = await post_mcp(client, token["access_token"], INITIALIZE)
    assert refused.status_code == 401


@pytest.mark.anyio
async def test_owner_set_password_writes_a_salted_hash(config: AuthConfig, capsys: pytest.CaptureFixture[str]):
    assert main(["owner", "set-password", "--password", PASSWORD]) == 0
    assert "owner password set" in capsys.readouterr().out
    first = (config.auth_dir / "auth.sqlite").read_bytes()
    assert PASSWORD.encode() not in first
    assert main(["owner", "set-password", "--password", PASSWORD]) == 0
    assert (config.auth_dir / "auth.sqlite").read_bytes() != first
    assert main(["owner", "set-password", "--password", "  "]) == 2


@pytest.mark.anyio
async def test_tokens_are_listed_and_revoked_from_the_command_line(
    client: httpx2.AsyncClient, capsys: pytest.CaptureFixture[str]
):
    registration = await register(client, name="Claude Code")
    token = await token_for(client, registration)
    capsys.readouterr()

    assert main(["tokens", "list"]) == 0
    listed = capsys.readouterr().out
    assert registration["client_id"] in listed
    assert "Claude Code" in listed
    assert "vault:read vault:write" in listed
    assert "issued " in listed and "expires " in listed
    assert "revoked" not in listed

    assert main(["tokens", "revoke", registration["client_id"]]) == 0
    assert "revoked 2 tokens" in capsys.readouterr().out
    assert (await post_mcp(client, token["access_token"], INITIALIZE)).status_code == 401

    assert main(["tokens", "list"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("revoked")
