from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from mcp.client import Client
from test_notes import VALID, body_of
from test_server import text_of

from notes_vault_mcp import notes
from notes_vault_mcp.frontmatter import validate
from notes_vault_mcp.index import Index
from notes_vault_mcp.server import build_server
from notes_vault_mcp.vault import Vault

NO_AREA = "---\ntitle: Utan area\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [x]\nstatus: active\n---\n\nKropp.\n"


@pytest.fixture
def no_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(self, force=False):
        raise AssertionError("the index was synced instead of updated by the write")

    monkeypatch.setattr(Index, "sync", refuse)


# --- every write updates the index itself ---------------------------------------------------------------------


def test_append_updates_the_index_without_a_sync(vault: Vault, no_sync):
    notes.append(vault, "Areas/orchard.md", "Ny rad.")
    assert vault.index.note("Areas/orchard.md").updated == notes.today()
    notes.append(vault, "Projects/brand-new.md", "Rad.")
    assert vault.index.note("Projects/brand-new.md") is not None


def test_close_updates_the_index_without_a_sync(vault: Vault, no_sync):
    destination = notes.close(vault, "Projects/orchard-fresh.md")
    assert vault.index.note("Projects/orchard-fresh.md") is None
    assert vault.index.note(destination).status == "complete"


def test_set_status_updates_the_index_without_a_sync(vault: Vault, no_sync):
    notes.set_status(vault, "Projects/orchard-fresh.md", "backlog", priority="high")
    row = vault.index.note("Projects/orchard-fresh.md")
    assert row.status == "backlog" and row.priority == "high"


def test_backlog_add_and_log_append_update_the_index_without_a_sync(vault: Vault, no_sync):
    key = notes.backlog_add(vault, "Idé", "orchard", "En rad", priority="low")
    assert vault.index.note(key).status == "backlog"
    notes.log_append(vault, "orchard", "Rad", commits=("abc1234",))
    assert "abc1234" in [row["sha"] for row in vault.index.db.execute("SELECT sha FROM shas")]


def test_delete_and_move_leave_no_stale_rows(vault: Vault, no_sync):
    notes.move(vault, "Areas/orchard.md", "Areas/moved.md")
    notes.delete(vault, "Areas/moved.md")
    keys = {row["key"] for row in vault.index.db.execute("SELECT key FROM notes")}
    fts = {row["key"] for row in vault.index.db.execute("SELECT key FROM notes_fts")}
    links = {row["src"] for row in vault.index.db.execute("SELECT src FROM links")}
    assert not {"Areas/orchard.md", "Areas/moved.md"} & (keys | fts | links)


@pytest.mark.xfail(
    strict=True,
    reason="bug: one SQLite connection is shared by the worker threads the MCP SDK runs sync tools in; "
    "concurrent writes raise DatabaseError",
)
def test_concurrent_writes_from_tool_threads_all_land_in_the_index(vault: Vault):
    def write(number: int) -> str:
        key = f"Projects/parallel-{number}.md"
        notes.write(vault, key, VALID.replace("Ny plan", f"Plan {number}"))
        return key

    with ThreadPoolExecutor(max_workers=8) as pool:
        keys = list(pool.map(write, range(40)))
    assert all(vault.index.note(key) is not None for key in keys)
    assert (
        vault.index.db.execute("SELECT count(*) FROM notes_fts WHERE key LIKE 'Projects/parallel-%'").fetchone()[0]
        == 40
    )


def test_a_rebuild_matches_the_incrementally_maintained_index(vault: Vault):
    notes.write(vault, "Projects/one.md", VALID)
    notes.append(vault, "Projects/one.md", "Mer.")
    notes.set_status(vault, "Projects/one.md", "backlog", priority="low")
    archived = notes.close(vault, "Projects/orchard-draft.md")
    before = {(row.key, row.status, row.updated) for row in vault.index.all_notes()}
    vault.index.rebuild()
    after = {(row.key, row.status, row.updated) for row in vault.index.all_notes()}
    assert before == after and any(key == archived for key, _, _ in after)


# --- the schema holds on every write path ------------------------------------------------------------------------


def test_a_body_without_frontmatter_is_refused_as_a_note(vault: Vault):
    with pytest.raises(notes.ValidationError):
        notes.write(vault, "Projects/bare.md", "Just text.\n")


@pytest.mark.xfail(
    strict=True,
    reason="bug: append_file creates a missing note without frontmatter, which the schema would refuse",
)
def test_append_cannot_create_a_note_the_schema_refuses(vault: Vault):
    notes.append(vault, "Projects/helt-ny.md", "Första raden.")
    frontmatter, _ = body_of(vault, "Projects/helt-ny.md")
    assert validate(frontmatter, "Projects/helt-ny.md", vault.schema) == []


@pytest.mark.xfail(
    strict=True,
    reason="bug: move_file skips validation, so a note can land in a folder whose rules it breaks",
)
def test_move_validates_the_note_against_the_destination_folder(vault: Vault):
    with pytest.raises(notes.ValidationError):
        notes.move(vault, "Areas/orchard.md", "Projects/orchard-task.md")


@pytest.mark.xfail(
    strict=True,
    reason="bug: close writes an archive note from a frontmatter-less file with only status and updated",
)
def test_close_of_a_note_without_frontmatter_yields_a_valid_archive_note(vault: Vault):
    destination = notes.close(vault, "Resources/no-frontmatter.md")
    frontmatter, _ = body_of(vault, destination)
    assert validate(frontmatter, destination, vault.schema) == []


@pytest.mark.xfail(strict=True, reason="bug: closing a note that already sits in the archive copies it to a -2 name")
def test_close_of_an_archived_note_keeps_its_key(vault: Vault):
    assert notes.close(vault, "Archive/old-plan.md") == "Archive/old-plan.md"
    assert vault.index.note("Archive/old-plan-2.md") is None


@pytest.mark.xfail(
    strict=True,
    reason="bug: a title with no latin letters slugifies to nothing and the key ends in a dash",
)
def test_backlog_add_with_a_non_latin_title_gets_a_usable_key(vault: Vault):
    key = notes.backlog_add(vault, "日本語のタイトル", "orchard", "En rad")
    assert not key.endswith("-.md")


@pytest.mark.xfail(
    strict=True,
    reason="bug: a note under .vault/ skips validation on write yet sync indexes it as a note",
)
def test_a_note_under_the_vault_folder_is_either_validated_or_never_indexed(vault: Vault):
    notes.write(vault, ".vault/sneaky.md", "no frontmatter\n")
    vault.index.sync(force=True)
    assert vault.index.note(".vault/sneaky.md") is None


def test_set_status_keeps_every_other_field(vault: Vault):
    before, _ = body_of(vault, "Projects/orchard-fresh.md")
    notes.set_status(vault, "Projects/orchard-fresh.md", "backlog", priority="medium")
    after, _ = body_of(vault, "Projects/orchard-fresh.md")
    changed = {key for key in before if before[key] != after.get(key)}
    assert changed <= {"status", "updated"}
    assert set(before) <= set(after)


def test_set_status_with_an_unknown_priority_is_refused(vault: Vault):
    with pytest.raises(notes.ValidationError):
        notes.set_status(vault, "Projects/orchard-fresh.md", "backlog", priority="asap")


@pytest.mark.anyio
async def test_rewriting_the_schema_through_write_file_does_not_loosen_the_running_server(vault: Vault):
    loose = "version: 1\nfrontmatter:\n  required: []\n  area_required_in: []\n"
    async with Client(build_server(vault)) as client:
        await text_of(client, "write_file", {"path": ".vault/schema.yml", "content": loose})
        result = await client.call_tool("write_file", {"path": "Projects/utan-area.md", "content": NO_AREA})
    assert result.is_error


@pytest.mark.anyio
async def test_a_read_of_a_binary_file_is_a_clean_tool_error(vault: Vault, vault_dir: Path):
    (vault_dir / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00")
    async with Client(build_server(vault)) as client:
        result = await client.call_tool("read_file", {"path": "pic.png"})
    assert result.is_error
    assert str(vault_dir) not in "".join(block.text for block in result.content)


@pytest.mark.anyio
async def test_a_tool_error_never_carries_the_vault_root(vault: Vault, vault_dir: Path):
    async with Client(build_server(vault)) as client:
        missing = await client.call_tool("read_file", {"path": "Areas/nope.md"})
        climbing = await client.call_tool("read_file", {"path": "../../etc/passwd"})
    for result in (missing, climbing):
        assert result.is_error
        assert str(vault_dir) not in "".join(block.text for block in result.content)
