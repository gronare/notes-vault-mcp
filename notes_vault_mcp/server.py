from __future__ import annotations

import secrets
from collections.abc import Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from notes_vault_mcp import notes
from notes_vault_mcp.backends import VaultError
from notes_vault_mcp.frontmatter import FrontmatterError
from notes_vault_mcp.schema import instructions
from notes_vault_mcp.search import render, search
from notes_vault_mcp.vault import Vault, open_vault

EXPECTED_FAILURES = (VaultError, FrontmatterError, notes.ValidationError)

SEARCH_DESCRIPTION = (
    "CHEAP — start here. Full-text search over the local SQLite index of the vault; it never "
    "downloads the whole vault. Matches title, summary, tags and body, folds diacritics, expands "
    'synonyms and accepts "quoted phrases". A bare commit sha looks up the notes that mention it. '
    "Excludes the archive and superseded notes unless you ask for them, and reports how many it hid."
)


def build_server(vault: Vault) -> MCPServer:
    server = MCPServer("vault", instructions=instructions(vault.schema), version="0.2.0")

    def run(action: Callable[[], str], force: bool = False) -> str:
        vault.index.sync(force=force)
        try:
            return action()
        except EXPECTED_FAILURES as exc:
            raise ToolError(str(exc)) from exc

    @server.tool(name="search", description=SEARCH_DESCRIPTION)
    def search_tool(
        query: str,
        limit: int = 15,
        folder: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        kind: str | None = None,
        area: str | None = None,
        include_archive: bool = False,
        include_superseded: bool = False,
        path_prefix: str | None = None,
        since: str | None = None,
    ) -> str:
        return run(
            lambda: render(
                search(
                    vault.index,
                    vault.schema,
                    query,
                    limit=limit,
                    folder=folder,
                    status=status,
                    tag=tag,
                    kind=kind,
                    area=area,
                    include_archive=include_archive,
                    include_superseded=include_superseded,
                    path_prefix=path_prefix,
                    since=since,
                )
            )
        )

    @server.tool(
        name="read_file",
        description=(
            "MODERATE — one round trip to storage. Returns the note with an `etag:` first line; pass "
            "that etag back to write_file to refuse a write over someone else's change. Search first."
        ),
    )
    def read_file(path: str) -> str:
        return run(lambda: notes.read(vault, path))

    @server.tool(
        name="write_file",
        description=(
            "WRITE — validates the frontmatter against the vault schema and refuses the write if it "
            "does not hold. Stamps `updated` with today and fills `date` when missing. Pass "
            "expected_etag from read_file to make the write conditional."
        ),
    )
    def write_file(path: str, content: str, expected_etag: str | None = None) -> str:
        return run(lambda: notes.write(vault, path, content, expected_etag=expected_etag))

    @server.tool(
        name="append_file",
        description="WRITE — appends to a note and bumps `updated`. Creates the note when it is missing.",
    )
    def append_file(path: str, content: str) -> str:
        return run(lambda: notes.append(vault, path, content))

    @server.tool(name="move_file", description="WRITE — moves or renames a note. Use `close` to archive finished work.")
    def move_file(source: str, dest: str) -> str:
        return run(lambda: notes.move(vault, source, dest))

    @server.tool(name="delete_file", description="WRITE — deletes a note for good. Prefer `close`, which keeps it.")
    def delete_file(path: str) -> str:
        return run(lambda: notes.delete(vault, path))

    @server.tool(
        name="list_files",
        description="CHEAP — paths only, no metadata and no bodies. `search` answers context questions better.",
    )
    def list_files(prefix: str = "") -> str:
        return run(lambda: _listing(vault, prefix))

    @server.tool(
        name="close",
        description=(
            "WRITE — finishes a note: sets status complete (or superseded with superseded_by when "
            "merged_into is given) and moves it into the archive. Run it in the same pass as the last "
            "commit of the work; a note left open is what makes the vault drift."
        ),
    )
    def close_tool(path: str, merged_into: str | None = None, status: str | None = None) -> str:
        return run(lambda: f"Closed: {notes.close(vault, path, merged_into=merged_into, status=status)}")

    @server.tool(
        name="backlog_add",
        description=(
            "WRITE — files an idea as a backlog note in the task folder: title, area (stem or [[stem]]), "
            "one line saying what and why, optional priority (urgent, high, medium, low) and source (who "
            "said it and when, or a sha). Call it the moment something is deferred, in so many words or in "
            "passing; picking the idea up later is setting its status to active."
        ),
    )
    def backlog_add(title: str, area: str, line: str, priority: str | None = None, source: str | None = None) -> str:
        return run(lambda: f"Filed: {notes.backlog_add(vault, title, area, line, priority=priority, source=source)}")

    @server.tool(
        name="backlog",
        description=(
            "CHEAP — the backlog sorted by priority then age. Filter by area (a stem, or a family such as "
            "greenhouse) and by priority."
        ),
    )
    def backlog_tool(area: str | None = None, priority: str | None = None, limit: int = 50) -> str:
        return run(lambda: notes.render_backlog(notes.backlog(vault, area=area, priority=priority)[:limit]))

    @server.tool(
        name="log_append",
        description=(
            "WRITE — appends one dated line to the repo log, with the commits it produced. Run once at "
            "the end of a session per repo. Creates the log note when it is missing. area is the stem of "
            "the system note (or [[stem]]); it defaults to the repo name."
        ),
    )
    def log_append(repo: str, line: str, commits: list[str] | None = None, area: str | None = None) -> str:
        return run(lambda: notes.log_append(vault, repo, line, commits=tuple(commits or ()), area=area))

    @server.tool(
        name="context",
        description=(
            "CHEAP — the session-start call. Returns the system notes covering the code at `path`, the "
            "open tasks for it, the reference notes and the tail of the repo log, in one answer. Use it "
            "before searching blind."
        ),
    )
    def context_tool(
        path: str | None = None,
        repo: str | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> str:
        return run(lambda: notes.context(vault, path=path, repo=repo, query=query, limit=limit).render())

    @server.tool(
        name="lint",
        description=(
            "MODERATE — reads every note to report drift: broken frontmatter, missing required fields, "
            "missing area, unresolved wikilinks, orphans, stale open tasks, archived notes still marked "
            "active, duplicate stems and superseded targets that do not exist."
        ),
    )
    def lint_tool() -> str:
        return run(lambda: notes.lint(vault).render(), force=True)

    return server


def _listing(vault: Vault, prefix: str) -> str:
    keys = [entry.key for entry in vault.backend.list() if entry.key.startswith(prefix)]
    return "\n".join(keys) if keys else f"No files under '{prefix}'"


class BearerToken:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    def _authorized(self, scope: dict) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                return secrets.compare_digest(value.decode("latin-1"), f"Bearer {self.token}")
        return False


def run_stdio(vault: Vault) -> None:
    build_server(vault).run("stdio")


def run_http(vault: Vault, host: str, port: int, token: str) -> None:
    import uvicorn

    app = build_server(vault).streamable_http_app(host=host)
    uvicorn.run(BearerToken(app, token), host=host, port=port, log_level="info")


def create_server() -> MCPServer:
    return build_server(open_vault())
