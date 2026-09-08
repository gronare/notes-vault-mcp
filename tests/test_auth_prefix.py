from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from test_auth_oidc import NOTE, Idp, oidc_app, session

from notes_vault_mcp.auth import AuthConfig
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.vault import Vault

PUBLIC_URL = "http://localhost/home-vault"
MCP_PATH = "/home-vault/mcp"
METADATA_PATH = "/.well-known/oauth-protected-resource/home-vault/mcp"
AUTHORIZATION_SERVER_METADATA_PATH = "/.well-known/oauth-authorization-server/home-vault"


@pytest.fixture
def idp() -> Idp:
    return Idp()


def builtin_app(vault: Vault, auth_dir: Path) -> Any:
    config = AuthConfig(mode="builtin", public_url=PUBLIC_URL, issuer=PUBLIC_URL, auth_dir=auth_dir)
    return build_http_app(vault, config)


@pytest.mark.anyio
async def test_the_protected_resource_metadata_sits_at_the_root_well_known_path(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, "", MCP_PATH) as opened:
        response = await opened.client.get(METADATA_PATH)
    assert response.status_code == 200
    assert response.json()["resource"] == "http://localhost/home-vault/mcp"


@pytest.mark.anyio
async def test_the_metadata_names_the_issuer_and_both_scopes(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, "", MCP_PATH) as opened:
        metadata = (await opened.client.get(METADATA_PATH)).json()
    assert metadata["authorization_servers"] == [idp.issuer]
    assert metadata["scopes_supported"] == ["vault:read", "vault:write"]


@pytest.mark.anyio
async def test_the_mcp_endpoint_under_the_prefix_refuses_a_request_without_a_token(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, "", MCP_PATH) as opened:
        response = await opened.post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert response.status_code == 401
    assert f'resource_metadata="http://localhost{METADATA_PATH}"' in response.headers["www-authenticate"]


@pytest.mark.anyio
async def test_a_read_token_reaches_the_tools_under_the_prefix(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, idp.token(groups=["vault"]), MCP_PATH) as opened:
        await opened.initialize()
        listing = await opened.call(2, "tools/list", {})
    assert "search" in {tool["name"] for tool in listing["result"]["tools"]}


@pytest.mark.anyio
async def test_a_write_token_writes_through_the_prefix(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, idp.token(groups=["vault-writers"]), MCP_PATH) as opened:
        await opened.initialize()
        result = await opened.tool("write_file", {"path": "Areas/prefix.md", "content": NOTE})
    assert not result.get("isError")


@pytest.mark.anyio
async def test_nothing_answers_outside_the_prefix(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, "", MCP_PATH) as opened:
        response = await opened.client.get("/mcp")
    assert response.status_code == 404


@pytest.mark.anyio
async def test_the_builtin_login_publishes_its_metadata_at_the_root_well_known_path(vault: Vault, tmp_path: Path):
    app = builtin_app(vault, tmp_path / "auth")
    async with session(app, "", MCP_PATH) as opened:
        response = await opened.client.get(AUTHORIZATION_SERVER_METADATA_PATH)
    assert response.status_code == 200
    metadata = response.json()
    assert metadata["issuer"] == PUBLIC_URL
    assert metadata["authorization_endpoint"] == f"{PUBLIC_URL}/authorize"
    assert metadata["token_endpoint"] == f"{PUBLIC_URL}/token"


@pytest.mark.anyio
async def test_an_external_provider_publishes_no_authorization_server_metadata(vault: Vault, idp: Idp, monkeypatch):
    app = oidc_app(vault, idp, monkeypatch, public_url=PUBLIC_URL)
    async with session(app, "", MCP_PATH) as opened:
        response = await opened.client.get(AUTHORIZATION_SERVER_METADATA_PATH)
    assert response.status_code == 404
