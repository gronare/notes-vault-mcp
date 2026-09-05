from __future__ import annotations

import pytest
from mcp.client import Client

from notes_vault_mcp.server import BearerToken, build_server
from notes_vault_mcp.vault import Vault

TOOL_NAMES = {
    "search",
    "read_file",
    "write_file",
    "append_file",
    "move_file",
    "delete_file",
    "list_files",
    "close",
    "log_append",
    "context",
    "lint",
}


async def text_of(client: Client, name: str, arguments: dict) -> str:
    result = await client.call_tool(name, arguments)
    return "".join(block.text for block in result.content if getattr(block, "text", None))


@pytest.mark.anyio
async def test_the_server_exposes_exactly_the_designed_tools(vault: Vault):
    async with Client(build_server(vault)) as client:
        tools = await client.list_tools()
    assert {tool.name for tool in tools.tools} == TOOL_NAMES


@pytest.mark.anyio
async def test_every_tool_description_states_its_cost(vault: Vault):
    async with Client(build_server(vault)) as client:
        tools = await client.list_tools()
    assert all(tool.description.startswith(("CHEAP", "MODERATE", "WRITE")) for tool in tools.tools)


@pytest.mark.anyio
async def test_the_instructions_carry_the_schema(vault: Vault):
    server = build_server(vault)
    assert "Areas/" in server.instructions
    assert "status is one of: draft, active, complete, superseded" in server.instructions


@pytest.mark.anyio
async def test_search_returns_rendered_rows(vault: Vault):
    async with Client(build_server(vault)) as client:
        text = await text_of(client, "search", {"query": "greenhouse", "limit": 3})
    assert text.startswith("3 of 5 (archive: 2 hidden, superseded: 0 hidden)")


@pytest.mark.anyio
async def test_write_then_read_round_trips(vault: Vault):
    content = (
        '---\ntitle: Via MCP\ndate: 2026-08-01\nupdated: 2026-08-01\n'
        'tags: [greenhouse]\nstatus: active\narea: "[[greenhouse]]"\n---\n\nKropp.\n'
    )
    async with Client(build_server(vault)) as client:
        written = await text_of(client, "write_file", {"path": "Projects/via-mcp.md", "content": content})
        read = await text_of(client, "read_file", {"path": "Projects/via-mcp.md"})
    assert written.startswith("Written: Projects/via-mcp.md")
    assert "title: Via MCP" in read


@pytest.mark.anyio
async def test_a_rejected_write_reports_the_problem(vault: Vault):
    content = "---\ntitle: Utan area\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [x]\nstatus: active\n---\n\nKropp.\n"
    async with Client(build_server(vault)) as client:
        result = await client.call_tool("write_file", {"path": "Projects/utan-area.md", "content": content})
    assert result.is_error
    assert "missing area" in "".join(block.text for block in result.content)


@pytest.mark.anyio
async def test_context_and_lint_return_text(vault: Vault):
    async with Client(build_server(vault)) as client:
        context = await text_of(client, "context", {"repo": "greenhouse"})
        findings = await text_of(client, "lint", {})
    assert "## open tasks" in context
    assert "## stale_active" in findings


async def _collect(app, headers: list[tuple[bytes, bytes]]) -> int:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await app({"type": "http", "headers": headers, "path": "/mcp", "method": "POST"}, receive, send)
    return sent[0]["status"]


class _Unreached:
    def __init__(self) -> None:
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


@pytest.mark.anyio
async def test_http_rejects_a_request_without_the_token():
    inner = _Unreached()
    assert await _collect(BearerToken(inner, "s3cret"), []) == 401
    assert inner.called is False


@pytest.mark.anyio
async def test_http_rejects_the_wrong_token():
    assert await _collect(BearerToken(_Unreached(), "s3cret"), [(b"authorization", b"Bearer nope")]) == 401


@pytest.mark.anyio
async def test_http_passes_the_right_token_through():
    inner = _Unreached()
    assert await _collect(BearerToken(inner, "s3cret"), [(b"authorization", b"Bearer s3cret")]) == 200
    assert inner.called is True
