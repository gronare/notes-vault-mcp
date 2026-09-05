from __future__ import annotations

from notes_vault_mcp.search import render, search
from notes_vault_mcp.vault import Vault


def keys(result) -> list[str]:
    return [row.note.key for row in result.rows]


def run(vault: Vault, query: str, **kwargs):
    return search(vault.index, vault.schema, query, **kwargs)


def test_terms_are_anded(vault: Vault):
    found = keys(run(vault, "greenhouse bokning"))
    assert "Resources/booking-trap.md" in found
    assert "Projects/greenhouse-draft.md" not in found
    assert "Areas/homelab.md" not in found


def test_synonyms_from_the_vault_schema_are_expanded(vault: Vault):
    assert keys(run(vault, "komponent")) == ["Projects/greenhouse-fresh.md"]


def test_terms_match_as_prefixes(vault: Vault):
    assert "Projects/greenhouse-fresh.md" in keys(run(vault, "bokning"))


def test_diacritics_are_folded(vault: Vault):
    assert "Areas/greenhouse.md" in keys(run(vault, "domän"))


def test_a_sha_looks_up_the_notes_that_mention_it(vault: Vault):
    assert keys(run(vault, "9ca31382")) == ["Resources/booking-trap.md"]


def test_a_sha_prefix_finds_the_same_note(vault: Vault):
    assert keys(run(vault, "9ca3138")) == ["Resources/booking-trap.md"]


def test_archive_is_hidden_and_counted(vault: Vault):
    result = run(vault, "greenhouse")
    assert result.hidden_archive == 2
    assert not any(key.startswith("Archive/") for key in keys(result))


def test_archive_is_returned_when_asked_for(vault: Vault):
    result = run(vault, "greenhouse", include_archive=True)
    assert result.hidden_archive == 0
    assert "Archive/old-plan.md" in keys(result)


def test_superseded_is_hidden_and_counted(vault: Vault):
    result = run(vault, "domän")
    assert result.hidden_superseded == 1
    assert "Resources/old-domain-howto.md" not in keys(result)


def test_superseded_is_returned_when_asked_for(vault: Vault):
    result = run(vault, "domän", include_superseded=True)
    assert "Resources/old-domain-howto.md" in keys(result)


def test_folder_weight_puts_the_system_note_first(vault: Vault):
    assert keys(run(vault, "flottkod"))[0] == "Areas/greenhouse.md"


def test_limit_is_respected(vault: Vault):
    result = run(vault, "greenhouse", limit=2)
    assert len(result.rows) == 2
    assert result.total == 5


def test_folder_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "greenhouse", folder="Log")) == ["Log/greenhouse-log.md"]


def test_status_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "greenhouse", status="draft")) == ["Projects/greenhouse-draft.md"]


def test_tag_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "greenhouse", tag="ui")) == ["Projects/greenhouse-fresh.md"]


def test_kind_filter_narrows_the_result(vault: Vault):
    assert keys(run(vault, "greenhouse", kind="system")) == ["Areas/greenhouse.md"]


def test_path_prefix_filter_narrows_the_result(vault: Vault):
    found = keys(run(vault, "release", path_prefix="~/projects/greenhouse"))
    assert "Areas/homelab.md" not in found
    assert "Projects/greenhouse-draft.md" in found


def test_a_quoted_phrase_stays_a_phrase(vault: Vault):
    assert keys(run(vault, '"stomme-addons och"')) == ["Projects/greenhouse-fresh.md"]


def test_render_states_the_counts_in_the_header(vault: Vault):
    text = render(run(vault, "greenhouse", limit=2))
    assert text.splitlines()[0] == "2 of 5 (archive: 2 hidden, superseded: 0 hidden)"


def test_render_says_so_when_nothing_matched(vault: Vault):
    assert "No notes matched." in render(run(vault, "kvantdator"))
