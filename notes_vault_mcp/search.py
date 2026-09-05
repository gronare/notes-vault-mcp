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


@dataclass
class SearchResult:
    rows: list[Row] = field(default_factory=list)
    total: int = 0
    hidden_archive: int = 0
    hidden_superseded: int = 0


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


def match_expression(query: str, schema: Schema) -> str:
    groups = []
    for term, is_phrase in terms_of(query):
        alternatives = _alternatives(term, is_phrase, schema)
        groups.append("(" + " OR ".join(alternatives) + ")")
    return " AND ".join(groups)


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


def _fts_rows(index: Index, schema: Schema, query: str) -> list[tuple[Note, float, str]]:
    expression = match_expression(query, schema)
    if not expression:
        return [(note, 1.0, _clean_snippet(note.summary)) for note in index.all_notes()]
    title_w, summary_w, tags_w, body_w = schema.bm25_weights
    weights = f"{title_w}, {summary_w}, {tags_w}, {body_w}, 0.0"
    sql = f"""
        SELECT notes_fts.key AS key,
               -bm25(notes_fts, {weights}) AS base,
               snippet(notes_fts, 3, '', '', '…', 20) AS snip
        FROM notes_fts WHERE notes_fts MATCH ?
    """
    rows = index.db.execute(sql, (expression,)).fetchall()
    found: list[tuple[Note, float, str]] = []
    for row in rows:
        note = index.note(row["key"])
        if note is not None:
            found.append((note, max(row["base"], 0.0001), _clean_snippet(row["snip"] or note.summary)))
    return found


def _sha_rows(index: Index, query: str) -> list[tuple[Note, float, str]] | None:
    terms = terms_of(query)
    if len(terms) != 1 or terms[0][1] or not SHA_TOKEN_RE.match(terms[0][0]):
        return None
    found = []
    for key in index.keys_for_sha(terms[0][0]):
        note = index.note(key)
        if note is not None:
            found.append((note, 1.0, _clean_snippet(note.summary)))
    return found


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
    candidates = _sha_rows(index, query)
    if candidates is None:
        candidates = _fts_rows(index, schema, query)

    now = time.time()
    unsearched = set(schema.unsearched_folders)
    result = SearchResult()
    visible: list[Row] = []
    for note, base, snippet in candidates:
        if not _matches_filters(note, folder, status, tag, kind, area, path_prefix, since):
            continue
        if note.folder in unsearched and not include_archive:
            result.hidden_archive += 1
            continue
        if _is_superseded(note) and not include_superseded:
            result.hidden_superseded += 1
            continue
        visible.append(Row(note=note, score=score_of(note, base, schema, now), snippet=snippet))

    visible.sort(key=lambda row: (-row.score, row.note.key))
    result.total = len(visible)
    result.rows = visible[:limit]
    return result


def render_row(row: Row, now: float) -> str:
    note = row.note
    age = humanize_age(age_days(note, now))
    area = note.area or "-"
    status = note.status or "-"
    return f"{note.key} | {note.title} | {age} | {status} | {area} | {row.snippet}"


def render(result: SearchResult) -> str:
    now = time.time()
    header = (
        f"{len(result.rows)} of {result.total} "
        f"(archive: {result.hidden_archive} hidden, superseded: {result.hidden_superseded} hidden)"
    )
    if not result.rows:
        return header + "\nNo notes matched."
    return "\n".join([header, *[render_row(row, now) for row in result.rows]])
