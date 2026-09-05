from __future__ import annotations

from pathlib import Path

from notes_vault_mcp.index import link_targets, path_list, shas_in
from notes_vault_mcp.vault import Vault


class CountingBackend:
    def __init__(self, inner):
        self.inner = inner
        self.fetched: list[str] = []

    def list(self):
        return self.inner.list()

    def get(self, key):
        self.fetched.append(key)
        return self.inner.get(key)

    def put(self, key, text, expected_version=None):
        return self.inner.put(key, text, expected_version)

    def delete(self, key):
        self.inner.delete(key)

    def move(self, src, dst):
        self.inner.move(src, dst)


def test_sync_indexes_every_markdown_note(vault: Vault):
    keys = {note.key for note in vault.index.all_notes()}
    assert "Areas/greenhouse.md" in keys
    assert "Archive/old-plan.md" in keys
    assert not any(key.endswith(".base") or key.startswith(".obsidian") for key in keys)


def test_sync_records_frontmatter_fields(vault: Vault):
    note = vault.index.note("Projects/greenhouse-fresh.md")
    assert note.title == "Greenhouse: bokningswidget i checkout"
    assert note.status == "active"
    assert note.area == "[[greenhouse]]"
    assert note.tags == ["greenhouse", "booking", "ui"]
    assert note.folder == "Projects"
    assert note.stem == "greenhouse-fresh"


def test_sync_marks_broken_frontmatter_invalid(vault: Vault):
    note = vault.index.note("Resources/broken-yaml.md")
    assert note.valid is False
    assert note.error


def test_sync_is_throttled_until_forced(vault: Vault):
    assert vault.index.sync() == 0


def test_sync_refetches_only_the_changed_note(vault: Vault, vault_dir: Path):
    counting = CountingBackend(vault.index.backend)
    vault.index.backend = counting
    changed = vault_dir / "Areas" / "homelab.md"
    changed.write_text(changed.read_text(encoding="utf-8") + "\nEn ny rad.\n", encoding="utf-8")
    assert vault.index.sync(force=True) == 1
    assert counting.fetched == ["Areas/homelab.md"]


def test_sync_removes_a_deleted_note(vault: Vault, vault_dir: Path):
    (vault_dir / "Areas" / "homelab.md").unlink()
    vault.index.sync(force=True)
    assert vault.index.note("Areas/homelab.md") is None


def test_sync_stores_paths_both_as_written_and_expanded(vault: Vault):
    paths = vault.index.note("Areas/homelab.md").paths
    assert "~/homelab" in paths
    assert str(Path("~/homelab").expanduser()) in paths


def test_rebuild_reindexes_everything(vault: Vault):
    before = len(vault.index.all_notes())
    assert vault.index.rebuild() == before
    assert len(vault.index.all_notes()) == before


def test_shas_need_a_letter_and_a_digit():
    assert shas_in("9ca31382 deadbeef 1234567 abcdefa f00d1234") == ["9ca31382", "f00d1234"]


def test_link_targets_drop_alias_heading_and_folder():
    assert link_targets("[[greenhouse]] [[Areas/Homelab|hem]] [[note#rubrik]]") == ["greenhouse", "homelab", "note"]


def test_path_list_splits_on_comma_space():
    assert path_list("~/a, /b")[:1] == ["~/a"]
    assert "/b" in path_list("~/a, /b")
