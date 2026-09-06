from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import date
from pathlib import Path

from notes_vault_mcp import changelog
from notes_vault_mcp.git import git, repo_name, repo_root
from notes_vault_mcp.notes import OPEN_STATUSES, context, vault_paths_in
from notes_vault_mcp.search import age_days, humanize_age
from notes_vault_mcp.vault import Vault, open_vault

STOP_STALE_DAYS = 14
SHA_PREFIX_LENGTH = 7
CHANGELOG_META = "changelog_ran_on"
VAULT_HINT = (
    "## how to read the vault\n"
    "The notes above live in the vault, not on disk. Read a note with the vault MCP tool `read_file(path)`, "
    "find others with `search`, file an idea with `backlog_add`, and never look for the vault's files in the "
    "filesystem: a wikilink [[stem]] is `search` on the stem, not a path."
)

__all__ = ["git", "repo_name", "repo_root"]


def read_hook_input(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


COMMITS_SHOWN = 10


def _commits_since(root: Path, since: str, paths: list[str]) -> str:
    if not since or not paths:
        return ""
    lines = git(["log", "--oneline", f"--since={since}", "--", *paths], root).splitlines()
    if len(lines) <= COMMITS_SHOWN:
        return "\n".join(lines)
    shown = "\n".join(lines[:COMMITS_SHOWN])
    return f"{len(lines)} commits, newest {COMMITS_SHOWN}:\n{shown}"


def session_start_text(vault: Vault, cwd: str) -> str:
    repo = repo_name(cwd)
    bundle = context(vault, path=cwd, repo=repo)
    blocks = [bundle.render()]
    root = repo_root(cwd)
    if root is not None:
        vault.index.remember_repo(repo, str(root))
        for note, _ in bundle.system:
            paths = vault_paths_in(note, root)
            commits = _commits_since(root, note.updated, paths)
            if commits:
                blocks.append(f"## commits since the note — {note.key} (updated {note.updated})\n{commits}")
    blocks.append(VAULT_HINT)
    return "\n\n".join(blocks)


def session_start(raw: str) -> str:
    data = read_hook_input(raw)
    cwd = str(data.get("cwd") or os.getcwd())
    vault = open_vault()
    try:
        vault.index.sync()
        return session_start_text(vault, cwd)
    finally:
        vault.close()


def _recent_commits(root: Path) -> list[tuple[str, str]]:
    output = git(["log", "--since=24 hours ago", "--format=%h|%s"], root)
    commits = []
    for line in output.splitlines():
        sha, _, subject = line.partition("|")
        if sha.strip():
            commits.append((sha.strip()[:SHA_PREFIX_LENGTH], subject.strip()))
    return commits


def _log_text(vault: Vault, repo: str) -> str:
    try:
        text, _ = vault.backend.get(vault.schema.log_key(repo))
    except Exception:
        return ""
    return text


def unlogged_commits(vault: Vault, root: Path, repo: str) -> list[tuple[str, str]]:
    logged = _log_text(vault, repo)
    return [(sha, subject) for sha, subject in _recent_commits(root) if sha not in logged]


def stale_open_notes(vault: Vault, repo: str, path: str) -> list[str]:
    now = time.time()
    task_folders = set(vault.schema.task_folders)
    bundle = context(vault, path=path, repo=repo)
    stale = []
    for note in bundle.tasks:
        if note.folder in task_folders and note.status in OPEN_STATUSES:
            days = age_days(note, now)
            if days > STOP_STALE_DAYS:
                stale.append(f"{note.key} ({humanize_age(days)} old)")
    return stale


def _reason(commits: list[tuple[str, str]], stale: list[str], repo: str) -> str:
    parts = []
    if commits:
        listed = ", ".join(f"{sha} {subject}" for sha, subject in commits)
        shas = [sha for sha, _ in commits]
        parts.append(f"log_append(repo='{repo}', line='<what landed>', commits={shas}) — {listed}")
    if stale:
        parts.append("close these notes, or say in them what is still open: " + ", ".join(stale))
    return "The vault is behind this session. " + "; ".join(parts)


def refresh_month_pages(vault: Vault, today: date | None = None) -> list[str]:
    stamp = (today or date.today()).isoformat()
    if vault.index.meta(CHANGELOG_META) == stamp:
        return []
    vault.index.set_meta(CHANGELOG_META, stamp)
    vault.index.db.commit()
    return changelog.write_all(vault, periods=changelog.periods_due(today or date.today()))


def stop(raw: str) -> str | None:
    data = read_hook_input(raw)
    if data.get("stop_hook_active"):
        return None
    if (os.environ.get("VAULT_STOP_HOOK") or "").lower() == "off":
        return None
    cwd = str(data.get("cwd") or os.getcwd())
    root = repo_root(cwd)
    if root is None:
        return None
    repo = root.name
    vault = open_vault()
    try:
        vault.index.sync()
        with contextlib.suppress(Exception):
            refresh_month_pages(vault)
        commits = unlogged_commits(vault, root, repo)
        stale = stale_open_notes(vault, repo, cwd)
    finally:
        vault.close()
    if not commits and not stale:
        return None
    return json.dumps({"decision": "block", "reason": _reason(commits, stale, repo)})
