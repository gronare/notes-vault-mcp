from __future__ import annotations

from notes_vault_mcp.notes import lint, lint_note_text
from notes_vault_mcp.vault import Vault


def groups(vault: Vault) -> dict[str, list[str]]:
    return lint(vault).as_dict()


def test_broken_frontmatter_is_reported_with_the_line(vault: Vault):
    assert any("Resources/broken-yaml.md: line" in entry for entry in groups(vault)["broken_frontmatter"])


def test_missing_required_fields_are_reported_per_field(vault: Vault):
    found = groups(vault)["missing_required"]
    assert "Resources/no-frontmatter.md: title" in found
    assert "Resources/no-frontmatter.md: status" in found


def test_missing_area_is_its_own_finding(vault: Vault):
    assert any("Resources/no-frontmatter.md" in entry for entry in groups(vault)["missing_area"])


def test_unknown_tags_are_silent_while_the_vocabulary_is_open(vault: Vault):
    assert "unknown_tags" not in groups(vault)


def test_unknown_tags_are_reported_when_strict(vault: Vault):
    vault.schema.data["tags"] = {"strict": True, "vocabulary": ["greenhouse"]}
    assert any("k3s" in entry for entry in groups(vault)["unknown_tags"])


def test_unresolved_links_name_the_source_and_the_target(vault: Vault):
    assert "Projects/greenhouse-draft.md -> [[saknad-not]]" in groups(vault)["unresolved_links"]


def test_orphans_ignore_the_log_and_archive_folders(vault: Vault):
    orphans = groups(vault)["orphans"]
    assert "Resources/no-frontmatter.md" in orphans
    assert not any(entry.startswith(("Log/", "Archive/")) for entry in orphans)


def test_stale_active_reports_an_old_open_task(vault: Vault):
    assert any("Projects/homelab-stale.md" in entry for entry in groups(vault)["stale_active"])


def test_a_fresh_open_task_is_not_stale(vault: Vault):
    assert not any("greenhouse-fresh" in entry for entry in groups(vault)["stale_active"])


def test_archive_status_mismatch_catches_an_archived_active_note(vault: Vault):
    assert groups(vault)["archive_status_mismatch"] == ["Archive/wrong-status.md: status active"]


def test_duplicate_stems_name_both_keys(vault: Vault):
    assert "homelab: Areas/homelab.md, Resources/homelab.md" in groups(vault)["duplicate_stems"]


def test_superseded_target_missing_is_reported(vault: Vault):
    assert groups(vault)["superseded_target_missing"] == ["Resources/old-domain-howto.md -> [[gone]]"]


def test_render_counts_the_findings(vault: Vault):
    text = lint(vault).render()
    assert text.splitlines()[0].endswith("findings")
    assert "## stale_active" in text


def test_the_lint_note_carries_a_complete_frontmatter(vault: Vault):
    text = lint_note_text(lint(vault))
    assert text.startswith("---\ntitle: Lint ")
    assert "kind: log" in text
    assert 'area: "[[vault]]"' in text


def test_the_repo_log_is_not_a_duplicate_stem(vault: Vault):
    assert not any(entry.startswith("greenhouse:") for entry in groups(vault)["duplicate_stems"])
