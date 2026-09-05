from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from notes_vault_mcp import notes
from notes_vault_mcp.backends import NotFound, VersionConflict
from notes_vault_mcp.frontmatter import FrontmatterError, parse
from notes_vault_mcp.vault import Vault

GREENHOUSE = str(Path("~/projects/greenhouse").expanduser())

VALID = """---
title: Ny plan
date: 2026-08-01
updated: 2026-08-01
tags: [greenhouse]
status: active
kind: task
area: "[[greenhouse]]"
---

Kroppen.
"""


def body_of(vault: Vault, key: str) -> tuple[dict, str]:
    text, _ = vault.backend.get(key)
    return parse(text)


def test_read_prefixes_the_etag(vault: Vault):
    text = notes.read(vault, "Areas/greenhouse.md")
    assert text.startswith("etag: ")


def test_read_warns_about_a_superseded_note(vault: Vault):
    assert "> [!warning] Superseded by [[gone]]" in notes.read(vault, "Resources/old-domain-howto.md")


def test_write_stamps_updated_with_today(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    frontmatter, _ = body_of(vault, "Projects/ny-plan.md")
    assert frontmatter["updated"] == date.today()
    assert frontmatter["date"] == date(2026, 8, 1)


def test_write_fills_a_missing_date(vault: Vault):
    content = VALID.replace("date: 2026-08-01\n", "")
    notes.write(vault, "Projects/ny-plan.md", content)
    frontmatter, _ = body_of(vault, "Projects/ny-plan.md")
    assert frontmatter["date"] == date.today()
    assert list(frontmatter)[:2] == ["title", "date"]


def test_write_indexes_the_note_at_once(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    assert vault.index.note("Projects/ny-plan.md").title == "Ny plan"


def test_write_refuses_a_note_without_area(vault: Vault):
    content = VALID.replace('area: "[[greenhouse]]"\n', "")
    with pytest.raises(notes.ValidationError) as caught:
        notes.write(vault, "Projects/ny-plan.md", content)
    assert "missing area" in str(caught.value)


def test_write_refuses_an_unknown_status(vault: Vault):
    content = VALID.replace("status: active", "status: halvklar")
    with pytest.raises(notes.ValidationError) as caught:
        notes.write(vault, "Projects/ny-plan.md", content)
    assert "halvklar" in str(caught.value)


def test_write_refuses_broken_yaml(vault: Vault):
    with pytest.raises(FrontmatterError):
        notes.write(vault, "Projects/ny-plan.md", "---\ntags: [unclosed\nstatus: active\n---\n\nkropp\n")


def test_write_refuses_a_stale_etag(vault: Vault):
    notes.write(vault, "Projects/ny-plan.md", VALID)
    _, stale = vault.backend.get("Projects/ny-plan.md")
    notes.write(vault, "Projects/ny-plan.md", VALID.replace("Kroppen.", "Nagon annan skrev."))
    with pytest.raises(VersionConflict):
        notes.write(vault, "Projects/ny-plan.md", VALID, expected_etag=stale)


def test_write_accepts_the_current_etag(vault: Vault):
    result = notes.write(vault, "Projects/ny-plan.md", VALID)
    etag = result.splitlines()[-1].removeprefix("etag: ")
    notes.write(vault, "Projects/ny-plan.md", VALID, expected_etag=etag)


def test_append_adds_a_line_and_bumps_updated(vault: Vault):
    notes.append(vault, "Areas/greenhouse.md", "En ny rad.")
    frontmatter, body = body_of(vault, "Areas/greenhouse.md")
    assert body.rstrip().endswith("En ny rad.")
    assert frontmatter["updated"] == date.today()


def test_append_creates_a_missing_note(vault: Vault):
    notes.append(vault, "Projects/helt-ny.md", "Första raden.")
    assert vault.backend.get("Projects/helt-ny.md")[0].strip() == "Första raden."


def test_move_reindexes_under_the_new_key(vault: Vault):
    notes.move(vault, "Projects/greenhouse-fresh.md", "Projects/omdopt.md")
    assert vault.index.note("Projects/greenhouse-fresh.md") is None
    assert vault.index.note("Projects/omdopt.md").title == "Greenhouse: bokningswidget i checkout"


def test_delete_removes_the_note_and_the_index_row(vault: Vault):
    notes.delete(vault, "Projects/greenhouse-fresh.md")
    assert vault.index.note("Projects/greenhouse-fresh.md") is None
    with pytest.raises(NotFound):
        vault.backend.get("Projects/greenhouse-fresh.md")


def test_close_moves_into_the_archive_and_completes(vault: Vault):
    destination = notes.close(vault, "Projects/greenhouse-fresh.md")
    assert destination == "Archive/greenhouse-fresh.md"
    frontmatter, _ = body_of(vault, destination)
    assert frontmatter["status"] == "complete"
    assert vault.index.note("Projects/greenhouse-fresh.md") is None


def test_close_with_merged_into_sets_superseded_by(vault: Vault):
    destination = notes.close(vault, "Projects/greenhouse-fresh.md", merged_into="Projects/greenhouse-draft.md")
    frontmatter, _ = body_of(vault, destination)
    assert frontmatter["status"] == "superseded"
    assert frontmatter["superseded_by"] == "[[greenhouse-draft]]"


def test_close_accepts_an_explicit_status(vault: Vault):
    destination = notes.close(vault, "Projects/greenhouse-fresh.md", status="draft")
    assert body_of(vault, destination)[0]["status"] == "draft"


def test_close_suffixes_a_name_the_archive_already_holds(vault: Vault):
    notes.close(vault, "Projects/greenhouse-fresh.md")
    notes.write(vault, "Projects/greenhouse-fresh.md", VALID)
    assert notes.close(vault, "Projects/greenhouse-fresh.md") == "Archive/greenhouse-fresh-2.md"


def test_log_append_adds_a_line_to_the_repo_log(vault: Vault):
    notes.log_append(vault, "greenhouse", "Bokningen klar", commits=("abc1234", "def5678"))
    _, body = body_of(vault, "Log/greenhouse-log.md")
    last = body.strip().splitlines()[-1]
    assert last == f"- [{date.today().isoformat()}] Bokningen klar | commits: abc1234 def5678 | [[greenhouse]]"


def test_log_append_creates_a_missing_log(vault: Vault):
    notes.log_append(vault, "stomme", "Första raden")
    frontmatter, body = body_of(vault, "Log/stomme-log.md")
    assert frontmatter["title"] == "Logg — stomme"
    assert frontmatter["kind"] == "log"
    assert frontmatter["tags"] == ["log", "stomme"]
    assert frontmatter["area"] == "[[stomme]]"
    assert "| commits: - |" in body


def test_log_append_takes_an_area_override(vault: Vault):
    notes.log_append(vault, "stomme", "rad", area="[[greenhouse]]")
    assert body_of(vault, "Log/stomme-log.md")[0]["area"] == "[[greenhouse]]"


def test_log_append_wraps_a_bare_area_stem_in_a_wikilink(vault: Vault):
    notes.log_append(vault, "stomme", "rad", area="greenhouse")
    frontmatter, body = body_of(vault, "Log/stomme-log.md")
    assert frontmatter["area"] == "[[greenhouse]]"
    assert body.rstrip().endswith("| [[greenhouse]]")


def test_context_returns_the_system_note_in_full(vault: Vault):
    bundle = notes.context(vault, path=GREENHOUSE, repo="greenhouse")
    assert [note.key for note, _ in bundle.system] == ["Areas/greenhouse.md"]
    assert "Kontrollplanet som styr motorn" in bundle.system[0][1]


def test_context_lists_only_open_tasks_for_the_repo(vault: Vault):
    bundle = notes.context(vault, path=GREENHOUSE, repo="greenhouse")
    assert {note.key for note in bundle.tasks} == {
        "Projects/greenhouse-fresh.md",
        "Projects/greenhouse-draft.md",
    }


def test_context_flags_a_stale_task(vault: Vault):
    text = notes.context(vault, path=str(Path("~/homelab").expanduser()), repo="homelab").render()
    assert "Projects/homelab-stale.md" in text
    assert "STALE" in text


def test_context_includes_the_reference_notes(vault: Vault):
    bundle = notes.context(vault, path=GREENHOUSE, repo="greenhouse")
    assert "Resources/booking-trap.md" in {note.key for note in bundle.references}


def test_context_ends_with_the_log_tail(vault: Vault):
    text = notes.context(vault, path=GREENHOUSE, repo="greenhouse").render()
    assert "## log — greenhouse" in text
    assert "Release-grinden mäter A/B" in text


def test_context_says_so_when_nothing_matches(vault: Vault):
    assert notes.context(vault, path="/opt/annat", repo="annat").render() == "No vault context for this path or repo."


def test_the_log_never_shares_a_stem_with_the_hub_note(vault: Vault):
    notes.log_append(vault, "greenhouse", "rad")
    stems = [note.stem for note in vault.index.all_notes()]
    assert stems.count("greenhouse") == 1
    assert "greenhouse-log" in stems


def test_the_log_filename_follows_the_schema_setting(vault: Vault):
    vault.schema.data["log"] = {**vault.schema.data["log"], "file_format": "logg-{repo}.md"}
    notes.log_append(vault, "stomme", "rad")
    assert vault.backend.get("Log/logg-stomme.md")[0].startswith("---")
    assert notes.log_lines(vault, "stomme", date.today().isoformat()[:7])


def test_context_puts_the_hub_note_before_a_draft_with_the_same_path(vault: Vault):
    notes.write(
        vault,
        "Areas/avtal-utkast.md",
        "---\ntitle: Avtal utkast\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [greenhouse]\nstatus: draft\n"
        "kind: system\npath: ~/projects/greenhouse\n---\n\nEtt utkast.\n",
    )
    bundle = notes.context(vault, path=GREENHOUSE, repo="greenhouse")
    assert [note.key for note, _ in bundle.system][0] == "Areas/greenhouse.md"


def test_context_reads_a_deeper_system_note_in_full_and_lists_root_level_ones_as_rows(vault: Vault):
    notes.write(
        vault,
        "Areas/greenhouse-bokning.md",
        "---\ntitle: Bokning\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [greenhouse]\nstatus: active\n"
        "kind: system\npath: ~/projects/greenhouse/app/models/bookings\n---\n\nBokningsdomänen.\n",
    )
    notes.write(
        vault,
        "Areas/greenhouse-personuppgifter.md",
        "---\ntitle: Personuppgifter\ndate: 2026-07-01\nupdated: 2026-07-01\ntags: [greenhouse]\nstatus: active\n"
        "kind: system\npath: ~/projects/greenhouse\n---\n\nInventering.\n",
    )
    at_root = notes.context(vault, path=GREENHOUSE, repo="greenhouse")
    assert [note.key for note, _ in at_root.system] == ["Areas/greenhouse.md"]
    assert {note.key for note in at_root.system_rows} >= {
        "Areas/greenhouse-bokning.md",
        "Areas/greenhouse-personuppgifter.md",
    }
    deep = notes.context(vault, path=f"{GREENHOUSE}/app/models/bookings", repo="greenhouse")
    assert [note.key for note, _ in deep.system] == ["Areas/greenhouse.md", "Areas/greenhouse-bokning.md"]
    assert "## other system notes" in at_root.render()
