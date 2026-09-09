from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2 as httpx
import pytest
from forwarded_support import HOST, ISSUER, TYP, Proxy, config

from notes_vault_mcp.auth import auth_config
from notes_vault_mcp.auth.forwarded import ForwardedVerifier, IdentityError
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.auth.personal import PersonalTokens
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.vault import SubjectVaults, subject_key

PROTOCOL_VERSION = "2025-06-18"
NOTE = "---\ntitle: Mine\ndate: 2026-09-01\nupdated: 2026-09-01\ntags: [work]\nstatus: active\n---\n\nBody.\n"


def app_for(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path, **overrides: Any) -> Any:
    return build_http_app(vaults, config(proxy, tmp_path, **overrides))


def _request(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


class Forwarded:
    def __init__(self, client: httpx.AsyncClient, token: str, path: str = "/mcp") -> None:
        self.client = client
        self.path = path
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if token:
            self.headers["X-Forwarded-Identity"] = token

    async def post(self, body: dict[str, Any]) -> httpx.Response:
        return await self.client.post(self.path, json=body, headers=self.headers)

    async def initialize(self) -> dict[str, Any]:
        params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        response = await self.post(_request(1, "initialize", params))
        assert response.status_code == 200, response.text
        return response.json()

    async def call(self, request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        response = await self.post(_request(request_id, method, params))
        assert response.status_code == 200, response.text
        return response.json()

    async def tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return (await self.call(2, "tools/call", {"name": name, "arguments": arguments}))["result"]


@asynccontextmanager
async def session(app: Any, token: str, path: str = "/mcp") -> AsyncIterator[Forwarded]:
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            yield Forwarded(client, token, path)


def text_of(result: dict[str, Any]) -> str:
    return "".join(block.get("text", "") for block in result.get("content", []))


# --- the verifier -----------------------------------------------------------------------------------------


def test_a_write_entitlement_grants_read_and_write(proxy: Proxy, tmp_path: Path):
    token = ForwardedVerifier(config(proxy, tmp_path)).verify(proxy.token())
    assert token.scopes == ["vault:read", "vault:write"]
    assert token.subject == "1042"
    assert token.client_id == HOST
    assert token.claims is not None and token.claims["email"] == "user1042@example.com"


def test_a_read_entitlement_grants_read_only(proxy: Proxy, tmp_path: Path):
    token = ForwardedVerifier(config(proxy, tmp_path)).verify(proxy.token(entitlements=("vault:read",)))
    assert token.scopes == ["vault:read"]


def test_the_entitlement_names_come_from_the_configuration(proxy: Proxy, tmp_path: Path):
    verifier = ForwardedVerifier(config(proxy, tmp_path, write_claims=("vault",), read_claims=("vault-viewer",)))
    assert verifier.verify(proxy.token(entitlements=("vault",))).scopes == ["vault:read", "vault:write"]
    assert verifier.verify(proxy.token(entitlements=("vault-viewer",))).scopes == ["vault:read"]


@pytest.mark.parametrize(
    ("claims", "error"),
    [
        ({"entitlements": ["support-mcp:admin"]}, "insufficient_scope"),
        ({"iss": "someone-else.example.com"}, "invalid_token"),
        ({"aud": "other-tenant.example.com"}, "invalid_token"),
        ({"exp": int(time.time()) - 5}, "invalid_token"),
        ({"typ": "mcp-proxy+access"}, "invalid_token"),
    ],
)
def test_a_token_that_does_not_fit_is_refused(proxy: Proxy, tmp_path: Path, claims: dict[str, Any], error: str):
    with pytest.raises(IdentityError) as caught:
        ForwardedVerifier(config(proxy, tmp_path)).verify(proxy.token(**claims))
    assert caught.value.error == error


def test_a_token_signed_by_another_key_is_refused(proxy: Proxy, tmp_path: Path):
    with pytest.raises(IdentityError) as caught:
        ForwardedVerifier(config(proxy, tmp_path)).verify(proxy.foreign_token())
    assert caught.value.error == "invalid_token"


# --- the configuration ------------------------------------------------------------------------------------


def _forwarded_env(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path, **extra: str) -> None:
    monkeypatch.setenv("VAULT_PUBLIC_URL", "https://vault-mcp.example.com")
    monkeypatch.setenv("VAULT_IDENTITY_PUBLIC_KEY", base64.b64encode(proxy.pem.encode("utf-8")).decode("ascii"))
    monkeypatch.setenv("VAULT_IDENTITY_ISSUER", ISSUER)
    monkeypatch.setenv("VAULT_AUTH_DIR", str(tmp_path / "auth"))
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_forwarded_reads_the_key_issuer_and_defaults(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path):
    _forwarded_env(monkeypatch, proxy, tmp_path)
    settings = auth_config("forwarded")
    assert settings.identity_public_key.startswith("-----BEGIN PUBLIC KEY-----")
    assert settings.identity_issuer == ISSUER
    assert settings.identity_header == "X-Forwarded-Identity"
    assert settings.identity_typ == ""
    assert settings.write_claims == ("vault:write",)
    assert settings.read_claims == ("vault:read",)
    assert settings.subject_prefix == "users"
    assert settings.public_host == "vault-mcp.example.com"
    assert settings.dav_url == "https://vault-mcp.example.com/dav/"


def test_forwarded_takes_header_typ_claims_and_prefix_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path
):
    _forwarded_env(
        monkeypatch,
        proxy,
        tmp_path,
        VAULT_IDENTITY_HEADER="X-Auctionet-Identity",
        VAULT_IDENTITY_TYP=TYP,
        VAULT_IDENTITY_WRITE_CLAIMS="vault vault:write",
        VAULT_IDENTITY_READ_CLAIMS="vault-viewer",
        VAULT_SUBJECT_PREFIX="/employees/",
    )
    settings = auth_config("forwarded")
    assert settings.identity_header == "X-Auctionet-Identity"
    assert settings.identity_typ == TYP
    assert settings.write_claims == ("vault", "vault:write")
    assert settings.read_claims == ("vault-viewer",)
    assert settings.subject_prefix == "employees"


def test_forwarded_accepts_a_raw_pem_key(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path):
    _forwarded_env(monkeypatch, proxy, tmp_path, VAULT_IDENTITY_PUBLIC_KEY=proxy.pem)
    assert auth_config("forwarded").identity_public_key == proxy.pem.strip()


def test_forwarded_refuses_to_start_without_a_key(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path):
    _forwarded_env(monkeypatch, proxy, tmp_path)
    monkeypatch.delenv("VAULT_IDENTITY_PUBLIC_KEY")
    with pytest.raises(ConfigError, match="VAULT_IDENTITY_PUBLIC_KEY"):
        auth_config("forwarded")


def test_forwarded_refuses_a_key_that_is_not_pem(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path):
    _forwarded_env(monkeypatch, proxy, tmp_path, VAULT_IDENTITY_PUBLIC_KEY="bm90IGEga2V5")
    with pytest.raises(ConfigError, match="not a PEM public key"):
        auth_config("forwarded")


def test_forwarded_refuses_to_start_without_an_issuer(monkeypatch: pytest.MonkeyPatch, proxy: Proxy, tmp_path: Path):
    _forwarded_env(monkeypatch, proxy, tmp_path)
    monkeypatch.delenv("VAULT_IDENTITY_ISSUER")
    with pytest.raises(ConfigError, match="VAULT_IDENTITY_ISSUER"):
        auth_config("forwarded")


# --- the http contract ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_health_route_answers_without_an_identity(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    async with session(app_for(vaults, proxy, tmp_path), "") as opened:
        response = await opened.client.get("/up")
    assert response.status_code == 200
    assert response.text == "ok"


@pytest.mark.anyio
async def test_a_request_without_the_header_gets_403_and_no_challenge(
    vaults: SubjectVaults, proxy: Proxy, tmp_path: Path
):
    async with session(app_for(vaults, proxy, tmp_path), "") as opened:
        response = await opened.post(_request(1, "initialize", {}))
    assert response.status_code == 403
    assert response.json() == {"error": "invalid_token", "error_description": "missing forwarded identity"}
    assert "www-authenticate" not in response.headers


@pytest.mark.anyio
async def test_a_foreign_signature_gets_403(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    async with session(app_for(vaults, proxy, tmp_path), proxy.foreign_token()) as opened:
        response = await opened.post(_request(1, "initialize", {}))
    assert response.status_code == 403
    assert response.json()["error"] == "invalid_token"


@pytest.mark.anyio
async def test_an_identity_without_the_entitlement_gets_403(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    token = proxy.token(entitlements=("support-mcp:admin",))
    async with session(app_for(vaults, proxy, tmp_path), token) as opened:
        response = await opened.post(_request(1, "initialize", {}))
    assert response.status_code == 403
    assert response.json()["error"] == "insufficient_scope"


@pytest.mark.anyio
async def test_the_header_name_comes_from_the_configuration(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    app = app_for(vaults, proxy, tmp_path, identity_header="X-Auctionet-Identity")
    async with session(app, "") as opened:
        opened.headers["X-Auctionet-Identity"] = proxy.token()
        listing = await opened.call(1, "tools/list", {})
    assert "obsidian_access" in {tool["name"] for tool in listing["result"]["tools"]}


@pytest.mark.anyio
async def test_responses_are_plain_json_without_a_session(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    async with session(app_for(vaults, proxy, tmp_path), proxy.token()) as opened:
        response = await opened.post(_request(1, "tools/list", {}))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    assert "search" in {tool["name"] for tool in response.json()["result"]["tools"]}


@pytest.mark.anyio
async def test_the_instructions_describe_the_english_template(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    async with session(app_for(vaults, proxy, tmp_path), proxy.token()) as opened:
        answer = await opened.initialize()
    assert "Tags are free-form." in answer["result"]["instructions"]


@pytest.mark.anyio
async def test_a_read_only_identity_cannot_write(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    token = proxy.token(entitlements=("vault:read",))
    async with session(app_for(vaults, proxy, tmp_path), token) as opened:
        result = await opened.tool("write_file", {"path": "Areas/x.md", "content": NOTE})
    assert result.get("isError")
    assert "may only read" in text_of(result)


# --- one vault per subject ----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_first_request_provisions_the_subjects_vault(
    vaults: SubjectVaults, proxy: Proxy, tmp_path: Path, storage: Path
):
    async with session(app_for(vaults, proxy, tmp_path), proxy.token(sub="1042")) as opened:
        listing = text_of(await opened.tool("list_files", {}))
    home = storage / "users" / "1042"
    assert (home / ".vault" / "schema.yml").read_text(encoding="utf-8").startswith("version: 1\nlanguage: en")
    assert (home / "Areas.base").exists() and (home / "Backlog.base").exists()
    welcome = (home / "Welcome.md").read_text(encoding="utf-8")
    assert "Remotely Save" in welcome and "http://localhost/dav/" in welcome and "{{" not in welcome
    assert "Welcome.md" in listing


@pytest.mark.anyio
async def test_two_subjects_never_see_each_others_notes(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    async with session(app_for(vaults, proxy, tmp_path), proxy.token(sub="1042")) as opened:
        written = await opened.tool("write_file", {"path": "Areas/anna.md", "content": NOTE})
        assert not written.get("isError"), text_of(written)
        assert "Areas/anna.md" in text_of(await opened.tool("list_files", {}))
        opened.headers["X-Forwarded-Identity"] = proxy.token(sub="1187")
        assert "Areas/anna.md" not in text_of(await opened.tool("list_files", {}))
        missing = await opened.tool("read_file", {"path": "Areas/anna.md"})
        assert missing.get("isError")
        assert "Welcome.md" in text_of(await opened.tool("list_files", {}))
        opened.headers["X-Forwarded-Identity"] = proxy.token(sub="1042")
        assert "Areas/anna.md" in text_of(await opened.tool("list_files", {}))


@pytest.mark.anyio
async def test_context_returns_the_welcome_note_for_a_fresh_subject(
    vaults: SubjectVaults, proxy: Proxy, tmp_path: Path
):
    async with session(app_for(vaults, proxy, tmp_path), proxy.token(sub="2001")) as opened:
        shown = text_of(await opened.tool("read_file", {"path": "Welcome.md"}))
    assert "title: Welcome to your vault" in shown
    assert "obsidian_access" in shown


@pytest.mark.anyio
async def test_obsidian_access_mints_a_personal_token_and_rotates_it(
    vaults: SubjectVaults, proxy: Proxy, tmp_path: Path
):
    tokens = PersonalTokens(tmp_path / "auth")
    async with session(app_for(vaults, proxy, tmp_path), proxy.token(sub="1042")) as opened:
        first = text_of(await opened.tool("obsidian_access", {}))
        second = text_of(await opened.tool("obsidian_access", {}))
    assert "Server address: http://localhost/dav/" in first
    assert "Username: user1042@example.com" in first
    secret_one = first.split("Password: ")[1].split("\n")[0]
    secret_two = second.split("Password: ")[1].split("\n")[0]
    assert tokens.subject_for(secret_one) is None
    assert tokens.subject_for(secret_two) == "1042"


@pytest.mark.anyio
async def test_a_read_only_identity_gets_no_personal_token(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    token = proxy.token(entitlements=("vault:read",))
    async with session(app_for(vaults, proxy, tmp_path), token) as opened:
        result = await opened.tool("obsidian_access", {})
    assert result.get("isError")


@pytest.mark.anyio
async def test_everything_sits_under_the_public_url_path(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path):
    app = app_for(vaults, proxy, tmp_path, public_url="http://localhost/vault")
    async with session(app, proxy.token(), path="/vault/mcp") as opened:
        assert (await opened.client.get("/vault/up")).status_code == 200
        listing = await opened.call(1, "tools/list", {})
        assert "search" in {tool["name"] for tool in listing["result"]["tools"]}
        assert (await opened.client.get("/vault/dav/")).status_code == 401


def test_a_subject_that_is_not_a_plain_identifier_is_hashed():
    assert subject_key("1042") == "1042"
    assert subject_key("carl.green@example.com") == "carl.green@example.com"
    hashed = subject_key("../../etc")
    assert len(hashed) == 16 and "/" not in hashed and "." not in hashed


def test_idle_vaults_are_closed_and_reopened_on_demand(storage: Path, tmp_path: Path):
    moment = [0.0]
    vaults = SubjectVaults(cache=tmp_path / "cache", idle_seconds=100, clock=lambda: moment[0])
    first = vaults.for_subject("1042")
    _ = first.index
    assert first.index_opened
    moment[0] = 50
    assert vaults.for_subject("1042") is first
    moment[0] = 200
    vaults.for_subject("1187")
    assert vaults.open_subjects == ["1187"]
    assert not first.index_opened
    assert vaults.for_subject("1042") is not first
    vaults.close()
    assert vaults.open_subjects == []


def test_provisioning_happens_once(storage: Path, tmp_path: Path):
    vaults = SubjectVaults(cache=tmp_path / "cache", dav_url="http://localhost/dav/")
    vault = vaults.for_subject("1042")
    vault.backend.put("Welcome.md", "edited by the owner")
    vaults.close()
    again = SubjectVaults(cache=tmp_path / "cache", dav_url="http://localhost/dav/").for_subject("1042")
    assert again.backend.get("Welcome.md")[0] == "edited by the owner"
    assert json.loads(json.dumps(again.schema.tag_vocabulary)) == []
