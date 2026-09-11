from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from notes_vault_mcp import changelog, hooks, notes, provision
from notes_vault_mcp.auth import auth_config
from notes_vault_mcp.config import ConfigError
from notes_vault_mcp.search import render, search
from notes_vault_mcp.server import run_http, run_stdio
from notes_vault_mcp.vault import open_vault

template = provision.template


def command_serve(args: argparse.Namespace) -> int:
    if args.transport != "http":
        run_stdio(open_vault())
        return 0
    auth = auth_config(args.auth)
    if auth.mode == "forwarded":
        from notes_vault_mcp.vault import SubjectVaults

        run_http(SubjectVaults(prefix=auth.subject_prefix, dav_url=auth.dav_url), args.host, args.port, auth)
        return 0
    run_http(open_vault(), args.host, args.port, auth)
    return 0


def command_init(args: argparse.Namespace) -> int:
    vault = open_vault()
    written, kept = provision.initialize(vault.backend, force=args.force)
    for key in kept:
        print(f"notes-vault-mcp: {key} exists, keeping it (--force overwrites)", file=sys.stderr)
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
    parked = notes.park_stale(vault) if args.park_stale else []
    findings = notes.lint(vault)
    for key in parked:
        findings.add("parked", f"{key}: status backlog, untouched for more than {vault.schema.stale_after_days} days")
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


def command_owner(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import builtin

    return builtin.command_owner(args)


def command_tokens(args: argparse.Namespace) -> int:
    from notes_vault_mcp.auth import builtin

    return builtin.command_tokens(args)


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
    serve.add_argument(
        "--auth",
        choices=("bearer", "oidc", "builtin", "forwarded"),
        default="bearer",
        help=(
            "http only: a static VAULT_TOKEN, an identity provider (VAULT_OIDC_ISSUER), the built-in owner login, "
            "or a signed identity header from a trusted proxy (VAULT_IDENTITY_*), one vault per subject"
        ),
    )
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
    lint.add_argument(
        "--park-stale",
        action="store_true",
        help="set status backlog on open task notes untouched for longer than the schema's stale_after_days",
    )
    lint.set_defaults(func=command_lint)

    sync = sub.add_parser("sync", help="refresh the index")
    sync.add_argument("--rebuild", action="store_true", help="drop the index and read every note again")
    sync.set_defaults(func=command_sync)

    owner = sub.add_parser("owner", help="the built-in login's owner (--auth builtin)")
    owner_sub = owner.add_subparsers(dest="owner_command", required=True)
    set_password = owner_sub.add_parser("set-password", help="set or replace the owner's password")
    set_password.add_argument("--password", help="read from the terminal when omitted")
    set_password.set_defaults(func=command_owner)

    tokens = sub.add_parser("tokens", help="the clients the built-in login has authorized")
    tokens_sub = tokens.add_subparsers(dest="tokens_command", required=True)
    tokens_sub.add_parser("list", help="list authorized clients and their tokens").set_defaults(func=command_tokens)
    revoke = tokens_sub.add_parser("revoke", help="revoke a client's tokens")
    revoke.add_argument("client_id")
    revoke.set_defaults(func=command_tokens)

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
