from __future__ import annotations

import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from notes_vault_mcp.index import Index, Note
from notes_vault_mcp.schema import Schema

SHA_TOKEN_RE = re.compile(r"^(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}$")
PREFIX_MIN_LENGTH = 4
SNIPPET_LENGTH = 160
STATUS_FACTORS = {"active": 1.0, "draft": 0.9, "complete": 0.7, "superseded": 0.3}
DAY_SECONDS = 86400.0


@dataclass
class Row:
    note: Note
    score: float
    snippet: str = ""
    terms: int = 0


@dataclass
class SearchResult:
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    hidden_archive: int = 0
    hidden_superseded: int = 0
    term_count: int = 0
    best_terms: int = 0

    @property
    def partial(self) -> bool:
        return self.term_count > 0


def terms_of(query: str) -> list[tuple[str, bool]]:
    try:
        tokens = shlex.split(query)
    except ValueError:
        tokens = query.split()
    terms: list[tuple[str, bool]] = []
    for token in tokens:
        text = token.strip()
        if text:
            terms.append((text, " " in text))
    return terms


def _quote(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _alternatives(term: str, is_phrase: bool, schema: Schema) -> list[str]:
    words = [term, *schema.synonyms_of(term)]
    alternatives: list[str] = []
    for word in words:
        alternatives.append(_quote(word))
        if not is_phrase and len(word) >= PREFIX_MIN_LENGTH:
            alternatives.append(f"{_quote(word)} *")
    return alternatives


def term_groups(query: str, schema: Schema) -> list[str]:
    return ["(" + " OR ".join(_alternatives(term, is_phrase, schema)) + ")" for term, is_phrase in terms_of(query)]


def match_expression(query: str, schema: Schema) -> str:
    return " AND ".join(term_groups(query, schema))


def stem_query(query: str) -> str | None:
    terms = terms_of(query)
    if len(terms) != 1:
        return None
    text = terms[0][0].strip()
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].split("|", 1)[0].split("#", 1)[0]
    stem = text.strip().rsplit("/", 1)[-1].removesuffix(".md")
    return stem or None


def parse_day(stamp: str) -> float | None:
    try:
        year, month, day = (int(part) for part in stamp[:10].split("-"))
    except ValueError:
        return None
    try:
        return datetime(year, month, day, tzinfo=UTC).timestamp()
    except ValueError:
        return None


def age_days(note: Note, now: float) -> float:
    moment = parse_day(note.updated or note.date)
    if moment is not None:
        return max(0.0, (now - moment) / DAY_SECONDS)
    if note.mtime:
        return max(0.0, (now - note.mtime) / DAY_SECONDS)
    return 0.0


def humanize_age(days: float) -> str:
    whole = int(days)
    if whole < 30:
        return f"{whole}d"
    if whole < 365:
        return f"{whole // 30}mo"
    return f"{whole // 365}y"


def recency_factor(days: float, half_life: float) -> float:
    return 0.5 + 0.5 * (2.0 ** (-days / half_life))


def status_factor(status: str) -> float:
    return STATUS_FACTORS.get(status, 1.0)


def score_of(note: Note, base: float, schema: Schema, now: float) -> float:
    weight = schema.folder_weight(note.folder)
    recency = recency_factor(age_days(note, now), schema.recency_half_life_days)
    return base * weight * recency * status_factor(note.status)


def _clean_snippet(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:SNIPPET_LENGTH]


def _is_superseded(note: Note) -> bool:
    return bool(note.superseded_by) or note.status == "superseded"


def _matches_filters(
    note: Note,
    folder: str | None,
    status: str | None,
    tag: str | None,
    kind: str | None,
    area: str | None,
    path_prefix: str | None,
    since: str | None,
) -> bool:
    if folder and note.folder != folder:
        return False
    if status and note.status != status:
        return False
    if tag and tag not in note.tags:
        return False
    if kind and note.kind != kind:
        return False
    if area and area.lower() not in note.area.lower():
        return False
    if path_prefix and not any(path.startswith(path_prefix) or path_prefix.startswith(path) for path in note.paths):
        return False
    return not (since and (note.updated or note.date) < since)


def _summary_row(note: Note) -> Row:
    return Row(note=note, score=1.0, snippet=_clean_snippet(note.summary))


def _fts_rows(index: Index, schema: Schema, query: str) -> list[Row]:
    expression = match_expression(query, schema)
    if not expression:
        return [_summary_row(note) for note in index.all_notes()]
    return _fts_query(index, schema, expression)


def _fts_query(index: Index, schema: Schema, expression: str) -> list[Row]:
    weights = ", ".join(str(weight) for weight in (*schema.bm25_weights, 0.0))
    sql = f"""
        SELECT notes_fts.key AS key,
               -bm25(notes_fts, {weights}) AS base,
               snippet(notes_fts, 3, '', '', '…', 20) AS snip
        FROM notes_fts WHERE notes_fts MATCH ?
    """
    rows = index.db.execute(sql, (expression,)).fetchall()
    found: list[Row] = []
    for row in rows:
        note = index.note(row["key"])
        if note is not None:
            found.append(
                Row(note=note, score=max(row["base"], 0.0001), snippet=_clean_snippet(row["snip"] or note.summary))
            )
    return found


def _matching_keys(index: Index, expression: str) -> set[str]:
    rows = index.db.execute("SELECT key FROM notes_fts WHERE notes_fts MATCH ?", (expression,)).fetchall()
    return {row["key"] for row in rows}


def _partial_rows(index: Index, schema: Schema, query: str) -> list[Row]:
    groups = term_groups(query, schema)
    found = _fts_query(index, schema, " OR ".join(groups))
    matched = [_matching_keys(index, group) for group in groups]
    for row in found:
        row.terms = sum(row.note.key in keys for keys in matched)
    return found


def _sha_rows(index: Index, query: str) -> list[Row] | None:
    terms = terms_of(query)
    if len(terms) != 1 or terms[0][1] or not SHA_TOKEN_RE.match(terms[0][0]):
        return None
    found = []
    for key in index.keys_for_sha(terms[0][0]):
        note = index.note(key)
        if note is not None:
            found.append(_summary_row(note))
    return found


def _candidates(index: Index, schema: Schema, query: str) -> tuple[list[Row], bool]:
    found = _sha_rows(index, query)
    if found is None:
        found = _fts_rows(index, schema, query)
    if found or len(terms_of(query)) < 2:
        return found, False
    return _partial_rows(index, schema, query), True


def _stem_rows(index: Index, query: str, candidates: list[Row]) -> list[Row]:
    stem = stem_query(query)
    if stem is None:
        return []
    by_key = {row.note.key: row for row in candidates}
    return [by_key.get(note.key) or _summary_row(note) for note in index.notes_with_stem(stem)]


def search(
    index: Index,
    schema: Schema,
    query: str,
    limit: int = 15,
    folder: str | None = None,
    status: str | None = None,
    tag: str | None = None,
    kind: str | None = None,
    area: str | None = None,
    include_archive: bool = False,
    include_superseded: bool = False,
    path_prefix: str | None = None,
    since: str | None = None,
) -> SearchResult:
    candidates, partial = _candidates(index, schema, query)
    pinned = _stem_rows(index, query, candidates)
    pinned_keys = {row.note.key for row in pinned}

    def wanted(note: Note) -> bool:
        return _matches_filters(note, folder, status, tag, kind, area, path_prefix, since)

    now = time.time()
    unsearched = set(schema.unsearched_folders)
    result = SearchResult()
    visible: list[Row] = []
    for row in candidates:
        note = row.note
        if note.key in pinned_keys or not wanted(note):
            continue
        if note.folder in unsearched and not include_archive:
            result.hidden_archive += 1
            continue
        if _is_superseded(note) and not include_superseded:
            result.hidden_superseded += 1
            continue
        row.score = score_of(note, row.score, schema, now)
        visible.append(row)

    visible.sort(key=lambda row: (-row.terms, -row.score, row.note.key))
    visible = [row for row in pinned if wanted(row.note)] + visible
    if partial and visible:
        result.term_count = len(terms_of(query))
        result.best_terms = max(row.terms for row in visible)
    result.total = len(visible)
    result.rows = visible[:limit]
    return result


def render_row(row: Row, now: float) -> str:
    note = row.note
    age = humanize_age(age_days(note, now))
    area = note.area or "-"
    status = note.status or "-"
    return f"{note.key} | {note.title} | {age} | {status} | {area} | {row.snippet}"


def render_header(result: SearchResult) -> str:
    hidden = f"(archive: {result.hidden_archive} hidden, superseded: {result.hidden_superseded} hidden)"
    if result.partial:
        terms = result.term_count
        return f"0 matched all {terms} terms; {result.total} match some (best {result.best_terms} of {terms}) {hidden}"
    return f"{len(result.rows)} of {result.total} {hidden}"


def render(result: SearchResult) -> str:
    now = time.time()
    header = render_header(result)
    if not result.rows:
        return header + "\nNo notes matched."
    return "\n".join([header, *[render_row(row, now) for row in result.rows]])
