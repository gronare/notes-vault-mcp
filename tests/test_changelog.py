from __future__ import annotations

import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_cli_hooks import feed, head_sha, make_repo

from notes_vault_mcp import changelog, hooks
from notes_vault_mcp.cli import main
from notes_vault_mcp.frontmatter import parse
from notes_vault_mcp.vault import Vault

THIS_MONTH = date.today().strftime("%Y-%m")


def commit(repo: Path, subject: str) -> str:
    (repo / "more.py").write_text(f"# {subject}\n", encoding="utf-8")
    subprocess.run(["git", "add", "more.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", subject], cwd=repo, check=True
    )
    return head_sha(repo)


def test_periods_due_adds_the_previous_month_in_the_first_week():
    assert changelog.periods_due(date(2026, 9, 3)) == ["2026-08", "2026-09"]
    assert changelog.periods_due(date(2026, 9, 20)) == ["2026-09"]
    assert changelog.periods_due(date(2027, 1, 2)) == ["2026-12", "2027-01"]


def test_period_key_follows_the_log_folder(vault: Vault):
    assert vault.schema.period_key("greenhouse", "2026-08") == "Log/greenhouse-2026-08.md"


def test_write_page_creates_the_month_page_with_the_log_area(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    assert changelog.write_page(vault, "greenhouse", "2026-08", repo) is True
    frontmatter, body = parse((vault_dir / "Log/greenhouse-2026-08.md").read_text(encoding="utf-8"))
    assert frontmatter["area"] == "[[greenhouse]]"
    assert frontmatter["status"] == "complete"
    assert frontmatter["kind"] == "log"
    assert changelog.GENERATED_START in body and changelog.GENERATED_END in body
    assert "Release-grinden mäter A/B" in body
    assert "[[greenhouse-fresh]]" in body


def test_write_page_lists_only_the_repos_own_notes(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "stomme")
    changelog.write_page(vault, "stomme", "2026-08", repo)
    body = (vault_dir / "Log/stomme-2026-08.md").read_text(encoding="utf-8")
    assert "[[greenhouse-fresh]]" not in body


def test_write_page_keeps_prose_outside_the_markers_and_refreshes_the_block(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    changelog.write_page(vault, "greenhouse", THIS_MONTH, repo)
    page = vault_dir / "Log/greenhouse-2026-08.md".replace("2026-08", THIS_MONTH)
    frontmatter, body = parse(page.read_text(encoding="utf-8"))
    assert frontmatter["status"] == "active"
    from notes_vault_mcp.frontmatter import dump

    page.write_text(dump(frontmatter, "## Sammanfattning\n\nEn månad av prosa.\n\n" + body), encoding="utf-8")
    vault.index.sync(force=True)
    sha = commit(repo, "Second commit")
    assert changelog.write_page(vault, "greenhouse", THIS_MONTH, repo) is True
    text = page.read_text(encoding="utf-8")
    assert "En månad av prosa." in text
    assert sha in text
    assert text.count(changelog.GENERATED_START) == 1


def test_write_page_is_a_no_op_when_nothing_changed(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    changelog.write_page(vault, "greenhouse", "2026-08", repo)
    before = (vault_dir / "Log/greenhouse-2026-08.md").read_text(encoding="utf-8")
    assert changelog.write_page(vault, "greenhouse", "2026-08", repo) is False
    assert (vault_dir / "Log/greenhouse-2026-08.md").read_text(encoding="utf-8") == before


def test_write_all_covers_the_remembered_repos_and_skips_a_vanished_checkout(vault: Vault, vault_dir: Path, tmp_path):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    vault.index.remember_repo("greenhouse", str(repo))
    vault.index.remember_repo("gone", str(tmp_path / "repos" / "gone"))
    written = changelog.write_all(vault, periods=["2026-08"])
    assert written == ["Log/greenhouse-2026-08.md"]
    assert not (vault_dir / "Log/gone-2026-08.md").exists()


def test_session_start_remembers_the_repo(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    assert ("greenhouse", str(repo.resolve())) in vault.index.known_repos()


def test_stop_writes_the_month_pages_once_a_day(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    vault.index.remember_repo("greenhouse", str(repo))
    page = vault_dir / f"Log/greenhouse-{THIS_MONTH}.md"
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"
    assert page.exists()
    page.unlink()
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert not page.exists()
    vault.index.set_meta(hooks.CHANGELOG_META, (date.today() - timedelta(days=1)).isoformat())
    vault.index.db.commit()
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert page.exists()


def test_changelog_all_writes_the_pages_for_a_named_period(vault: Vault, vault_dir: Path, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    vault.index.remember_repo("greenhouse", str(repo))
    assert main(["changelog", "--all", "2026-08"]) == 0
    assert "wrote Log/greenhouse-2026-08.md" in capsys.readouterr().out
    assert (vault_dir / "Log/greenhouse-2026-08.md").exists()


def test_changelog_write_writes_one_page(vault: Vault, vault_dir: Path, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    assert main(["changelog", "greenhouse", "2026-08", "--repo-path", str(repo), "--write"]) == 0
    assert "wrote Log/greenhouse-2026-08.md" in capsys.readouterr().out
    assert main(["changelog", "greenhouse", "2026-08", "--repo-path", str(repo), "--write"]) == 0
    assert "unchanged Log/greenhouse-2026-08.md" in capsys.readouterr().out


def test_changelog_without_a_repo_or_all_fails_loudly(vault: Vault, capsys):
    assert main(["changelog"]) == 2
