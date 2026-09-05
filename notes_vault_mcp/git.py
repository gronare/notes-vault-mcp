from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 10


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


def is_checkout(path: Path) -> bool:
    return (path / ".git").exists()
