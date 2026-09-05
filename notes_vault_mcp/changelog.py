from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from notes_vault_mcp.hooks import git
from notes_vault_mcp.notes import changelog_notes, log_lines
from notes_vault_mcp.vault import Vault

PERIOD_LENGTHS = {4: "year", 7: "month"}


def period_bounds(period: str) -> tuple[str, str]:
    if len(period) == 4:
        year = int(period)
        return f"{year}-01-01", f"{year + 1}-01-01"
    year, month = (int(part) for part in period.split("-"))
    end_year, end_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year}-{month:02d}-01", f"{end_year}-{end_month:02d}-01"


def git_commits(repo_path: Path, period: str) -> dict[str, list[str]]:
    since, until = period_bounds(period)
    output = git(["log", f"--since={since}", f"--until={until}", "--format=%h|%ad|%s", "--date=short"], repo_path)
    by_day: dict[str, list[str]] = defaultdict(list)
    for line in output.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            by_day[parts[1]].append(f"{parts[0]} {parts[2]}")
    return dict(sorted(by_day.items()))


def render(vault: Vault, repo: str, period: str, repo_path: Path | None) -> str:
    blocks = [f"# {repo} — {period}"]

    lines = log_lines(vault, repo, period)
    blocks.append("## log\n" + ("\n".join(lines) if lines else "- (no log lines)"))

    if repo_path is not None:
        by_day = git_commits(repo_path, period)
        if by_day:
            days = [f"### {day}\n" + "\n".join(f"- {entry}" for entry in entries) for day, entries in by_day.items()]
            blocks.append("## commits\n" + "\n".join(days))
        else:
            blocks.append("## commits\n- (no commits)")

    notes = sorted(changelog_notes(vault, period), key=lambda note: (note.date, note.key))
    if notes:
        rows = [f"- [[{note.stem}]] — {note.title} ({note.date}, {note.folder})" for note in notes]
        blocks.append("## notes\n" + "\n".join(rows))
    else:
        blocks.append("## notes\n- (no notes)")

    return "\n\n".join(blocks)
