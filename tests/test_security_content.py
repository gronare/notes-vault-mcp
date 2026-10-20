from __future__ import annotations

import pytest
from test_notes import body_of

from notes_vault_mcp import notes
from notes_vault_mcp.frontmatter import FrontmatterError, dump, parse, tags_of
from notes_vault_mcp.index import note_from_text
from notes_vault_mcp.search import search, terms_of
from notes_vault_mcp.vault import Vault

HEAD = "---\ntitle: Note\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [orchard]\nstatus: active\n"


def _aliased_frontmatter(levels: int, width: int = 10) -> str:
    lines = [f"a: &a [{', '.join(['x'] * width)}]"]
    previous = "a"
    for step in range(1, levels):
        name = chr(ord("a") + step)
        lines.append(f"{name}: &{name} [{', '.join(['*' + previous] * width)}]")
        previous = name
    return "\n".join(lines) + f"\ntitle: *{previous}"


# --- yaml -----------------------------------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="bug: YAML aliases in frontmatter expand without bound; 8 levels make a 500 MB title, 9 exhaust memory",
)
def test_aliased_frontmatter_cannot_blow_up_the_index():
    text = f"---\n{_aliased_frontmatter(6)}\n---\nbody\n"
    note, _ = note_from_text("Areas/laughs.md", text, "v", 0.0, len(text))
    assert len(note.title) < 10_000


def test_a_yaml_tag_cannot_build_arbitrary_objects():
    text = "---\ntitle: !!python/object/apply:os.system ['echo pwned']\n---\nbody\n"
    with pytest.raises(FrontmatterError):
        parse(text)


def test_a_frontmatter_that_is_a_list_is_rejected_not_crashed():
    with pytest.raises(FrontmatterError):
        parse("---\n- a\n- b\n---\nbody\n")


def test_a_broken_note_is_indexed_as_invalid_rather_than_breaking_the_sync(vault: Vault, vault_dir):
    (vault_dir / "Areas" / "odd.md").write_text("---\ntitle: {\n---\nbody\n", encoding="utf-8")
    vault.index.sync(force=True)
    note = vault.index.note("Areas/odd.md")
    assert note is not None and note.valid is False and note.error.startswith("line ")


@pytest.mark.xfail(
    strict=True,
    reason="bug: a scalar tags value makes tags_of raise TypeError, which aborts the sync of the whole vault",
)
def test_a_note_with_a_scalar_tags_value_does_not_break_the_sync(vault: Vault, vault_dir):
    (vault_dir / "Areas" / "odd.md").write_text("---\ntitle: Odd\ntags: 5\n---\nbody\n", encoding="utf-8")
    vault.index.sync(force=True)
    assert vault.index.note("Areas/odd.md") is not None


@pytest.mark.parametrize("raw", [5, True, 3.5])
@pytest.mark.xfail(strict=True, reason="bug: tags_of raises TypeError on a scalar tags value")
def test_tags_of_tolerates_a_scalar(raw):
    assert isinstance(tags_of({"tags": raw}), list)


def test_frontmatter_values_of_every_type_index_as_text(vault: Vault, vault_dir):
    text = (
        "---\ntitle: [a, b]\nsummary: {k: v}\nstatus: true\nkind: 2026-01-01\narea: 7\n"
        "path: {a: 1}\ndate: 2026-01-01 10:00:00\nupdated: null\nsuperseded_by: [x]\npriority: 1.5\n---\nbody\n"
    )
    (vault_dir / "Areas" / "typed.md").write_text(text, encoding="utf-8")
    vault.index.sync(force=True)
    note = vault.index.note("Areas/typed.md")
    assert (
        note is not None
        and note.title == "a, b"
        and note.status.lower() == "true"
        and note.date.startswith("2026-01-01")
    )


# --- dump round trips -----------------------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="bug: dump writes a key containing ': ' unquoted, and the note no longer parses")
def test_a_key_containing_a_colon_survives_a_rewrite(vault: Vault):
    notes.write(vault, "Areas/colon.md", HEAD + '"odd: key": 1\n---\n\nBody.\n')
    frontmatter, _ = body_of(vault, "Areas/colon.md")
    assert frontmatter["odd: key"] == 1


@pytest.mark.xfail(
    strict=True,
    reason="bug: dump stringifies a nested mapping, so Obsidian property objects are destroyed on set_status",
)
def test_a_nested_mapping_survives_set_status(vault: Vault, vault_dir):
    (vault_dir / "Projects" / "nested.md").write_text(
        HEAD + 'kind: task\narea: "[[orchard]]"\nmeta:\n  owner: carl\n---\n\nBody.\n', encoding="utf-8"
    )
    vault.index.sync(force=True)
    notes.set_status(vault, "Projects/nested.md", "backlog", priority="low")
    frontmatter, _ = body_of(vault, "Projects/nested.md")
    assert frontmatter["meta"] == {"owner": "carl"}


@pytest.mark.xfail(
    strict=True,
    reason="bug: dump folds a block scalar onto one line, altering multi-line summaries on every rewrite",
)
def test_a_block_scalar_survives_a_rewrite(vault: Vault):
    notes.write(vault, "Areas/block.md", HEAD + "summary: |\n  line one\n  line two\n---\n\nBody.\n")
    frontmatter, _ = body_of(vault, "Areas/block.md")
    assert frontmatter["summary"] == "line one\nline two\n"


PLAIN_STRINGS = [
    "yes", "no", "null", "~", "2026-01-01", "a: b", "a #b", "[x]", "{x}", "'q'", '"q"', "*star", "&anchor", "!tag",
    "|pipe", ">gt", "%p", "@at", "`bt", ", comma", "ö och å", "back\\slash", "1e3", "1:30", "", " padded ",
]  # fmt: skip
RETYPED_STRINGS = ["+1", ".5", "0x1F", "1_000", "017", "0b101"]


def _round_trip(value: str) -> tuple[str, list[str]]:
    frontmatter, _ = parse(dump({"title": value, "tags": [value]}, "body\n"))
    return str(frontmatter["title"]), [str(tag) for tag in frontmatter["tags"]]


@pytest.mark.parametrize("value", PLAIN_STRINGS)
def test_dump_writes_a_string_that_reads_back_as_the_same_string(value: str):
    assert _round_trip(value) == (value, [value])


@pytest.mark.parametrize("value", RETYPED_STRINGS)
@pytest.mark.xfail(strict=True, reason="bug: a numeric-looking string is written unquoted and reads back as a number")
def test_dump_quotes_a_string_yaml_would_read_as_a_number(value: str):
    assert _round_trip(value) == (value, [value])


@pytest.mark.xfail(
    strict=True,
    reason="bug: a value starting with '- ' is written unquoted and the note no longer parses",
)
def test_dump_quotes_a_string_starting_like_a_sequence_entry():
    assert _round_trip("- dash") == ("- dash", ["- dash"])


# --- search ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "orchard OR grove",
        "NEAR(orchard grove)",
        "title:orchard",
        "orchard*",
        "*",
        "(",
        ")",
        '"',
        'a"b',
        "orchard AND",
        "-orchard",
        "^orchard",
        "orchard NOT grove",
        "{orchard}",
        "stem : orchard",
        "orchard;DROP TABLE notes;--",
        "' OR 1=1 --",
        "\\",
        "a" * 5000,
        " ".join(["orchard"] * 200),
        "🔥",
    ],
)
def test_any_query_text_is_a_literal_search_and_never_an_error(vault: Vault, query: str):
    result = search(vault.index, vault.schema, query)
    assert result.total >= 0
    assert vault.index.note("Areas/orchard.md") is not None


@pytest.mark.xfail(
    strict=True,
    reason="bug: a NUL byte in the query reaches FTS5 and raises OperationalError: unterminated string",
)
def test_a_nul_byte_in_the_query_is_a_literal_search_too(vault: Vault):
    assert search(vault.index, vault.schema, "orchard\u0000").total >= 0


def test_a_query_that_looks_like_an_fts_column_filter_is_still_literal(vault: Vault):
    assert search(vault.index, vault.schema, "title:orchard").total == 0


def test_filters_are_compared_in_python_never_in_sql(vault: Vault):
    result = search(vault.index, vault.schema, "orchard", folder="Areas' OR '1'='1", status="x; DROP TABLE notes")
    assert result.total == 0
    assert vault.index.note("Areas/orchard.md") is not None


def test_limit_zero_counts_but_returns_no_rows(vault: Vault):
    result = search(vault.index, vault.schema, "orchard", limit=0)
    assert result.rows == [] and result.total > 0


@pytest.mark.xfail(
    strict=True,
    reason="bug: a since value that is not ISO is compared as text and silently hides every note",
)
def test_a_non_iso_since_is_refused_or_ignored(vault: Vault):
    result = search(vault.index, vault.schema, "orchard", since="yesterday")
    assert result.total > 0


def test_terms_of_survives_an_unbalanced_quote():
    assert terms_of('orchard "grove') == [("orchard", False), ('"grove', False)]


def test_a_huge_note_indexes_and_reads_truncated_in_context(vault: Vault, vault_dir):
    body = "ord " * 200_000
    (vault_dir / "Areas" / "big.md").write_text(
        f"{HEAD}kind: system\npath: ~/projects/big\n---\n\n{body}", encoding="utf-8"
    )
    vault.index.sync(force=True)
    rendered = notes.context(vault, path=str(vault_dir.home() / "projects" / "big")).render()
    assert notes.TRUNCATED_MARKER in rendered
    assert len(rendered) < notes.CONTEXT_TRUNCATE + 2000
