from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from notes_vault_mcp.notes import OPEN_STATUSES, context, vault_paths_in
from notes_vault_mcp.search import age_days, humanize_age
from notes_vault_mcp.vault import Vault, open_vault

GIT_TIMEOUT = 10
STOP_STALE_DAYS = 14
SHA_PREFIX_LENGTH = 7


def git(args: list[str], cwd: str | Path) -> str:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def repo_root(cwd: str | Path) -> Path | None:
    top = git(["rev-parse", "--show-toplevel"], cwd)
    return Path(top) if top else None


def repo_name(cwd: str | Path) -> str:
    root = repo_root(cwd)
    return (root or Path(cwd)).name


def read_hook_input(raw: str) -> dict:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _commits_since(root: Path, since: str, paths: list[str]) -> str:
    if not since or not paths:
        return ""
    return git(["log", "--oneline", f"--since={since}", "--", *paths], root)


def session_start_text(vault: Vault, cwd: str) -> str:
    repo = repo_name(cwd)
    bundle = context(vault, path=cwd, repo=repo)
    blocks = [bundle.render()]
    root = repo_root(cwd)
    if root is None:
        return "\n\n".join(blocks)
    for note, _ in bundle.system:
        paths = vault_paths_in(note, root)
        commits = _commits_since(root, note.updated, paths)
        if commits:
            blocks.append(f"## commits since the note — {note.key} (updated {note.updated})\n{commits}")
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
        commits = unlogged_commits(vault, root, repo)
        stale = stale_open_notes(vault, repo, cwd)
    finally:
        vault.close()
    if not commits and not stale:
        return None
    return json.dumps({"decision": "block", "reason": _reason(commits, stale, repo)})
