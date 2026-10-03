from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx2
import pytest

from notes_vault_mcp.auth import AuthConfig
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.vault import Vault

TOKEN = "s3cret"
CLUSTER_HOST = "home-vault-agents.home-vault.svc.cluster.local"
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "MCP-Protocol-Version": "2025-06-18",
    "Authorization": f"Bearer {TOKEN}",
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "bearer-auth-test", "version": "1"},
    },
}


@asynccontextmanager
async def serving(vault: Vault, host: str = "127.0.0.1", **overrides):
    app = build_http_app(vault, AuthConfig(mode="bearer", bearer_token=TOKEN, **overrides), host)
    async with app.app.router.lifespan_context(app.app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://localhost:8765") as http:
            yield http


async def initialize(client: httpx2.AsyncClient, host: str) -> httpx2.Response:
    return await client.post("/mcp", headers={**MCP_HEADERS, "host": host}, content=json.dumps(INITIALIZE))


@pytest.mark.anyio
async def test_a_service_host_is_served_when_the_server_listens_on_every_address(vault: Vault):
    async with serving(vault, host="0.0.0.0") as client:
        assert (await initialize(client, CLUSTER_HOST)).status_code == 200


@pytest.mark.anyio
async def test_a_foreign_host_is_refused_when_the_server_listens_on_localhost(vault: Vault):
    async with serving(vault) as client:
        assert (await initialize(client, CLUSTER_HOST)).status_code == 421


@pytest.mark.anyio
async def test_an_allowed_host_is_served_while_the_rest_are_refused(vault: Vault):
    async with serving(vault, host="0.0.0.0", allowed_hosts=(CLUSTER_HOST,)) as client:
        assert (await initialize(client, CLUSTER_HOST)).status_code == 200
        assert (await initialize(client, f"{CLUSTER_HOST}:8765")).status_code == 200
        assert (await initialize(client, "evil.example.com")).status_code == 421


@pytest.mark.anyio
async def test_the_public_url_host_is_served_while_the_rest_are_refused(vault: Vault):
    async with serving(vault, host="0.0.0.0", public_url=f"http://{CLUSTER_HOST}:8765") as client:
        assert (await initialize(client, CLUSTER_HOST)).status_code == 200
        assert (await initialize(client, "evil.example.com")).status_code == 421


@pytest.mark.anyio
async def test_a_public_url_without_a_scheme_still_names_the_allowed_host(vault: Vault):
    async with serving(vault, host="0.0.0.0", public_url=CLUSTER_HOST) as client:
        assert (await initialize(client, CLUSTER_HOST)).status_code == 200
        assert (await initialize(client, "evil.example.com")).status_code == 421


@pytest.mark.anyio
async def test_localhost_keeps_working_with_an_allowed_host_set(vault: Vault):
    async with serving(vault, host="0.0.0.0", allowed_hosts=(CLUSTER_HOST,)) as client:
        assert (await initialize(client, "localhost:8765")).status_code == 200


def test_run_http_builds_the_app_for_the_address_it_listens_on(vault: Vault, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from notes_vault_mcp import server
    from notes_vault_mcp.auth import http

    seen: dict[str, object] = {}
    monkeypatch.setattr(http, "build_http_app", lambda vaults, auth, host: seen.update(host=host))
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    server.run_http(vault, "0.0.0.0", 8765, AuthConfig(mode="bearer", bearer_token=TOKEN))
    assert seen == {"host": "0.0.0.0", "port": 8765, "log_level": "info"}
