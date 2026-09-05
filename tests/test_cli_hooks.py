from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from notes_vault_mcp import hooks, notes
from notes_vault_mcp.cli import main
from notes_vault_mcp.config import env
from notes_vault_mcp.vault import Vault

BACKEND_VARS = ("VAULT_PATH", "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET")


def make_repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-qm", "First commit"],
        cwd=repo,
        check=True,
    )
    return repo


def head_sha(repo: Path) -> str:
    args = ["git", "rev-parse", "--short=7", "HEAD"]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True).stdout.strip()


def feed(monkeypatch: pytest.MonkeyPatch, payload: dict) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def test_env_resolves_the_claude_plugin_option_prefix(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VAULT_TOKEN", "from-plugin")
    assert env("VAULT_TOKEN") == "from-plugin"


def test_env_prefers_the_bare_variable(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VAULT_TOKEN", "direct")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_VAULT_TOKEN", "from-plugin")
    assert env("VAULT_TOKEN") == "direct"


def test_session_start_prints_the_context(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    feed(monkeypatch, {"cwd": str(repo), "session_id": "abc"})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "Areas/greenhouse.md" in out
    assert "Projects/greenhouse-fresh.md" in out


def test_session_start_exits_zero_with_no_backend(monkeypatch: pytest.MonkeyPatch, capsys):
    for name in BACKEND_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_" + name, raising=False)
    feed(monkeypatch, {"cwd": "/tmp"})
    assert main(["hook", "session-start"]) == 0
    assert capsys.readouterr().out.startswith("vault: notes-vault-mcp: no vault configured")


def test_session_start_lists_commits_newer_than_the_note(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "stomme")
    note = vault_dir / "Areas" / "stomme.md"
    note.write_text(
        "---\ntitle: stomme\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [stomme]\nstatus: active\nkind: system\npath: {repo}\n---\n\nMotorn.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    out = capsys.readouterr().out
    assert "commits since the note — Areas/stomme.md" in out
    assert "First commit" in out


def test_stop_blocks_when_a_commit_is_not_logged(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "stomme")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"
    assert head_sha(repo) in payload["reason"]


def test_stop_is_silent_once_the_commit_is_logged(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "stomme")
    notes.log_append(vault, "stomme", "Första bygget", commits=(head_sha(repo),))
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_is_silent_when_the_hook_is_already_active(
    vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "stomme")
    feed(monkeypatch, {"cwd": str(repo), "stop_hook_active": True})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_is_silent_when_switched_off(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "stomme")
    monkeypatch.setenv("VAULT_STOP_HOOK", "off")
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_stop_blocks_on_a_stale_open_note(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "homelab")
    notes.log_append(vault, "homelab", "Bygget", commits=(head_sha(repo),))
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "stop"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "Projects/homelab-stale.md" in payload["reason"]


def test_stop_outside_a_git_repo_is_silent(vault: Vault, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys):
    loose = tmp_path / "loose"
    loose.mkdir()
    feed(monkeypatch, {"cwd": str(loose)})
    assert main(["hook", "stop"]) == 0
    assert capsys.readouterr().out == ""


def test_init_writes_the_schema_and_the_bases(vault: Vault, vault_dir: Path, capsys):
    assert main(["init"]) == 0
    assert (vault_dir / ".vault" / "schema.yml").exists()
    for name in ("Areas.base", "Open tasks.base", "Resources.base"):
        assert (vault_dir / name).exists()
    assert "## Vault" in capsys.readouterr().out


def test_init_keeps_an_existing_file_without_force(vault: Vault, vault_dir: Path):
    marker = "synonyms:\n  - [widget, komponent]\n"
    (vault_dir / ".vault" / "schema.yml").write_text(marker, encoding="utf-8")
    main(["init"])
    assert (vault_dir / ".vault" / "schema.yml").read_text(encoding="utf-8") == marker


def test_init_force_overwrites(vault: Vault, vault_dir: Path):
    (vault_dir / ".vault" / "schema.yml").write_text("synonyms: []\n", encoding="utf-8")
    main(["init", "--force"])
    assert "stale_after_days" in (vault_dir / ".vault" / "schema.yml").read_text(encoding="utf-8")


def test_search_command_prints_rows(vault: Vault, capsys):
    assert main(["search", "greenhouse", "--limit", "2"]) == 0
    assert capsys.readouterr().out.startswith("2 of 5")


def test_sync_command_reports_the_count(vault: Vault, capsys):
    assert main(["sync", "--rebuild"]) == 0
    assert "indexed 13 notes of 13" in capsys.readouterr().out


def test_lint_command_can_write_a_note(vault: Vault, vault_dir: Path, capsys):
    assert main(["lint", "--write", "Log/lint.md"]) == 0
    assert (vault_dir / "Log" / "lint.md").read_text(encoding="utf-8").startswith("---\ntitle: Lint ")


def test_changelog_prints_log_commits_and_notes(vault: Vault, tmp_path, capsys):
    repo = make_repo(tmp_path / "repos", "greenhouse")
    assert main(["changelog", "greenhouse", "2026-08", "--repo-path", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "# greenhouse — 2026-08" in out
    assert "Release-grinden mäter A/B" in out
    assert "## commits" in out
    assert "[[greenhouse-fresh]]" in out


def test_serve_rejects_http_without_a_token(vault: Vault, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_VAULT_TOKEN", raising=False)
    assert main(["serve", "--transport", "http"]) == 2
    assert "VAULT_TOKEN" in capsys.readouterr().err


def test_repo_name_falls_back_to_the_directory(tmp_path):
    loose = tmp_path / "not-a-repo"
    loose.mkdir()
    assert hooks.repo_name(loose) == "not-a-repo"


def test_session_start_follows_a_symlinked_repo_path(
    vault: Vault, vault_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path, capsys
):
    repo = make_repo(tmp_path / "repos", "stomme")
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "repos")
    note = vault_dir / "Areas" / "stomme.md"
    note.write_text(
        "---\ntitle: stomme\ndate: 2020-01-01\nupdated: 2020-01-01\n"
        f"tags: [stomme]\nstatus: active\nkind: system\npath: {link / 'stomme'}\n---\n\nMotorn.\n",
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    feed(monkeypatch, {"cwd": str(repo)})
    assert main(["hook", "session-start"]) == 0
    assert "commits since the note — Areas/stomme.md" in capsys.readouterr().out
