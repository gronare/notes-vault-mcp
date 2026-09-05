from __future__ import annotations

import argparse
import re
import sys
from importlib import resources
from pathlib import Path

from notes_vault_mcp import changelog, hooks, notes
from notes_vault_mcp.config import ConfigError, env
from notes_vault_mcp.search import render, search
from notes_vault_mcp.server import run_http, run_stdio
from notes_vault_mcp.vault import open_vault

TEMPLATES = ("schema.yml", "Areas.base", "Open tasks.base", "Resources.base", "Backlog.base")
SCHEMA_TARGET = ".vault/schema.yml"


def template(name: str) -> str:
    return resources.files("notes_vault_mcp.templates").joinpath(name).read_text(encoding="utf-8")


def _target_key(name: str) -> str:
    return SCHEMA_TARGET if name == "schema.yml" else name


def command_serve(args: argparse.Namespace) -> int:
    vault = open_vault()
    if args.transport == "http":
        token = env("VAULT_TOKEN")
        if not token:
            print("notes-vault-mcp: --transport http needs VAULT_TOKEN set", file=sys.stderr)
            return 2
        run_http(vault, args.host, args.port, token)
    else:
        run_stdio(vault)
    return 0


def command_init(args: argparse.Namespace) -> int:
    vault = open_vault()
    existing = {entry.key for entry in vault.backend.list()}
    written = []
    for name in TEMPLATES:
        key = _target_key(name)
        if key in existing and not args.force:
            print(f"notes-vault-mcp: {key} exists, keeping it (--force overwrites)", file=sys.stderr)
            continue
        vault.backend.put(key, template(name))
        written.append(key)
    for key in written:
        print(f"wrote {key}", file=sys.stderr)
    print(template("claude-md-snippet.md"))
    return 0


def command_hook(args: argparse.Namespace) -> int:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if args.event == "session-start":
        try:
            print(hooks.session_start(raw))
        except Exception as exc:
            print(f"vault: {exc}")
        return 0
    try:
        blocked = hooks.stop(raw)
    except Exception:
        return 0
    if blocked:
        print(blocked)
    return 0


PERIOD_RE = re.compile(r"^\d{4}(-\d{2})?$")


def command_changelog(args: argparse.Namespace) -> int:
    vault = open_vault()
    vault.index.sync()
    if args.all:
        period = args.period or (args.repo if args.repo and PERIOD_RE.match(args.repo) else None)
        for key in changelog.write_all(vault, periods=[period] if period else None):
            print(f"wrote {key}")
        return 0
    if not args.repo or not args.period:
        print("changelog needs <repo> <period>, or --all", file=sys.stderr)
        return 2
    repo_path = Path(args.repo_path).expanduser() if args.repo_path else None
    if args.write:
        changed = changelog.write_page(vault, args.repo, args.period, repo_path)
        print(("wrote " if changed else "unchanged ") + vault.schema.period_key(args.repo, args.period))
        return 0
    print(changelog.render(vault, args.repo, args.period, repo_path))
    return 0


def command_lint(args: argparse.Namespace) -> int:
    vault = open_vault()
    vault.index.sync(force=True)
    findings = notes.lint(vault)
    print(findings.render())
    if args.write:
        vault.backend.put(args.write, notes.lint_note_text(findings))
        print(f"wrote {args.write}", file=sys.stderr)
    return 0


def command_sync(args: argparse.Namespace) -> int:
    vault = open_vault()
    count = vault.index.rebuild() if args.rebuild else vault.index.sync(force=True)
    print(f"indexed {count} notes of {len(vault.index.all_notes())}")
    return 0


def command_backlog(args: argparse.Namespace) -> int:
    vault = open_vault()
    vault.index.sync()
    print(notes.render_backlog(notes.backlog(vault, area=args.area, priority=args.priority)))
    return 0


def command_search(args: argparse.Namespace) -> int:
    vault = open_vault()
    vault.index.sync()
    print(render(search(vault.index, vault.schema, args.query, limit=args.limit)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notes-vault-mcp",
        description="MCP server and CLI for a markdown notes vault on S3 or a local directory.",
    )
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the MCP server (default)")
    serve.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=command_serve)

    init = sub.add_parser("init", help="write the schema and the Obsidian bases into the vault")
    init.add_argument("--force", action="store_true", help="overwrite files that already exist")
    init.set_defaults(func=command_init)

    hook = sub.add_parser("hook", help="Claude Code hook entry points, reading the hook JSON on stdin")
    hook.add_argument("event", choices=("session-start", "stop"))
    hook.set_defaults(func=command_hook)

    log = sub.add_parser("changelog", help="print or write the log lines, commits and notes for a period")
    log.add_argument("repo", nargs="?")
    log.add_argument("period", nargs="?", help="YYYY-MM or YYYY")
    log.add_argument("--repo-path", help="the git checkout to read commits from")
    log.add_argument("--write", action="store_true", help="write the period page into the vault instead of printing")
    log.add_argument("--all", action="store_true", help="write the period pages for every repo the hooks have seen")
    log.set_defaults(func=command_changelog)

    lint = sub.add_parser("lint", help="report vault drift")
    lint.add_argument("--write", metavar="VAULT_PATH", help="also write the findings as a note")
    lint.set_defaults(func=command_lint)

    sync = sub.add_parser("sync", help="refresh the index")
    sync.add_argument("--rebuild", action="store_true", help="drop the index and read every note again")
    sync.set_defaults(func=command_sync)

    queue = sub.add_parser("backlog", help="list the backlog, sorted by priority then age")
    queue.add_argument("--area", help="a system note stem, or a family such as greenhouse")
    queue.add_argument("--priority", help="urgent, high, medium or low")
    queue.set_defaults(func=command_backlog)

    find = sub.add_parser("search", help="search the index from the shell")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=15)
    find.set_defaults(func=command_search)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["serve", *(argv or [])])
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
