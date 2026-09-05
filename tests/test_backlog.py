from __future__ import annotations

from pathlib import Path

import pytest

from notes_vault_mcp import notes
from notes_vault_mcp.cli import main
from notes_vault_mcp.frontmatter import parse
from notes_vault_mcp.vault import Vault


def test_backlog_add_files_a_note_under_the_area_family(vault: Vault, vault_dir: Path):
    key = notes.backlog_add(
        vault,
        "Hotellsök för flerrumssajter",
        "[[greenhouse-bokning]]",
        "Gästen väljer rum efter datum.",
        priority="high",
        source="Carl 2026-09-06",
    )
    assert key == "Projects/greenhouse-hotellsok-for-flerrumssajter.md"
    frontmatter, body = parse((vault_dir / key).read_text(encoding="utf-8"))
    assert frontmatter["status"] == "backlog"
    assert frontmatter["kind"] == "task"
    assert frontmatter["area"] == "[[greenhouse-bokning]]"
    assert frontmatter["priority"] == "high"
    assert frontmatter["source"] == "Carl 2026-09-06"
    assert frontmatter["tags"] == ["greenhouse"]
    assert body.strip() == "Gästen väljer rum efter datum."


def test_backlog_add_keeps_a_title_that_already_names_the_family(vault: Vault):
    key = notes.backlog_add(vault, "greenhouse — ordbokstrimmen", "greenhouse", "Dynamisk import av en/no-copy.")
    assert key == "Projects/greenhouse-ordbokstrimmen.md"


def test_backlog_add_refuses_an_unknown_priority(vault: Vault):
    with pytest.raises(ValueError, match="priority"):
        notes.backlog_add(vault, "Något", "greenhouse", "rad", priority="someday")


def test_backlog_add_refuses_a_missing_area(vault: Vault):
    with pytest.raises(ValueError, match="area"):
        notes.backlog_add(vault, "Något", "", "rad")


def test_backlog_lists_by_priority_then_age_and_filters_by_family(vault: Vault):
    notes.backlog_add(vault, "Lågt", "greenhouse-bokning", "rad", priority="low")
    notes.backlog_add(vault, "Högt", "greenhouse-portal", "rad", priority="high")
    notes.backlog_add(vault, "Utan", "stomme", "rad")
    notes.backlog_add(vault, "Bråttom", "stomme-motor", "rad", priority="urgent")
    titles = [note.title for note in notes.backlog(vault)]
    assert titles == ["Bråttom", "Högt", "Lågt", "Utan"]
    assert [note.title for note in notes.backlog(vault, area="greenhouse")] == ["Högt", "Lågt"]
    assert [note.title for note in notes.backlog(vault, area="[[stomme-motor]]")] == ["Bråttom"]
    assert [note.title for note in notes.backlog(vault, priority="low")] == ["Lågt"]


def test_render_backlog_reads_as_rows(vault: Vault):
    notes.backlog_add(vault, "Högt", "greenhouse-portal", "rad", priority="high")
    text = notes.render_backlog(notes.backlog(vault))
    assert text.startswith("1 backlog items")
    assert "Projects/greenhouse-hogt.md | Högt | high | [[greenhouse-portal]]" in text
    assert notes.render_backlog([]) == "The backlog is empty."


def test_context_shows_the_backlog_apart_from_the_open_tasks(vault: Vault):
    notes.backlog_add(vault, "Senare", "greenhouse", "rad", priority="medium")
    text = notes.context(vault, repo="greenhouse").render()
    assert "## backlog (1)" in text
    assert "Projects/greenhouse-senare.md | Senare | medium" in text
    assert "Senare" not in text.split("## backlog")[0].split("## open tasks")[-1]


def test_lint_leaves_a_backlog_note_alone_however_old(vault: Vault, vault_dir: Path):
    key = notes.backlog_add(vault, "Gammal idé", "greenhouse", "rad")
    path = vault_dir / key
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("updated: ", "updated: 2025-01-01 #", 1)
        .replace("2025-01-01 #", "2025-01-01\n#", 1),
        encoding="utf-8",
    )
    vault.index.sync(force=True)
    report = notes.lint(vault).render()
    assert "stale_active" not in report or key not in report


def test_lint_flags_a_finished_note_still_in_the_task_folder(vault: Vault, vault_dir: Path):
    path = vault_dir / "Projects/greenhouse-klar.md"
    frontmatter = "title: Klar\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [greenhouse]\nstatus: complete\nkind: task"
    path.write_text(f'---\n{frontmatter}\narea: "[[greenhouse]]"\n---\n\nKlart.\n', encoding="utf-8")
    vault.index.sync(force=True)
    report = notes.lint(vault).render()
    assert "done_not_closed" in report
    assert "Projects/greenhouse-klar.md" in report


def test_backlog_command_prints_the_rows(vault: Vault, capsys):
    notes.backlog_add(vault, "Högt", "greenhouse-portal", "rad", priority="high")
    assert main(["backlog", "--area", "greenhouse"]) == 0
    assert "Högt | high" in capsys.readouterr().out


def test_init_writes_the_backlog_base(vault: Vault, vault_dir: Path, capsys):
    assert main(["init", "--force"]) == 0
    assert (vault_dir / "Backlog.base").exists()
