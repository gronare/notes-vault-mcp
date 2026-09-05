from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from notes_vault_mcp.backends import Entry, VaultBackend
from notes_vault_mcp.frontmatter import FrontmatterError, folder_of, parse, tags_of

SYNC_THROTTLE_SECONDS = 20
FETCH_WORKERS = 16
SHA_RE = re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,40}\b")
WIKILINK_RE = re.compile(r"\[\[([^\]\n]+)\]\]")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS notes (
  key TEXT PRIMARY KEY, version TEXT, mtime REAL, folder TEXT, stem TEXT,
  title TEXT, summary TEXT, status TEXT, kind TEXT, area TEXT,
  tags TEXT, paths TEXT, date TEXT, updated TEXT, superseded_by TEXT,
  valid INT, error TEXT, size INT
);
CREATE INDEX IF NOT EXISTS notes_folder ON notes(folder);
CREATE INDEX IF NOT EXISTS notes_stem ON notes(stem);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  title, summary, tags, body, key UNINDEXED,
  tokenize="unicode61 remove_diacritics 2"
);
CREATE TABLE IF NOT EXISTS shas (sha TEXT, key TEXT);
CREATE INDEX IF NOT EXISTS shas_sha ON shas(sha);
CREATE TABLE IF NOT EXISTS links (src TEXT, target TEXT);
CREATE INDEX IF NOT EXISTS links_target ON links(target);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS repos (repo TEXT PRIMARY KEY, root TEXT, seen_at TEXT);
"""

NOTE_COLUMNS = (
    "key, version, mtime, folder, stem, title, summary, status, kind, area, "
    "tags, paths, date, updated, superseded_by, valid, error, size"
)


@dataclass
class Note:
    key: str
    version: str = ""
    mtime: float = 0.0
    folder: str = ""
    stem: str = ""
    title: str = ""
    summary: str = ""
    status: str = ""
    kind: str = ""
    area: str = ""
    tags: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    date: str = ""
    updated: str = ""
    superseded_by: str = ""
    valid: bool = True
    error: str = ""
    size: int = 0


def stem_of(key: str) -> str:
    return key.rsplit("/", 1)[-1].removesuffix(".md")


def link_targets(text: str) -> list[str]:
    targets = []
    for raw in WIKILINK_RE.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target:
            targets.append(target.rsplit("/", 1)[-1].lower())
    return sorted(set(targets))


def shas_in(text: str) -> list[str]:
    return sorted(set(SHA_RE.findall(text)))


def expand_home(path: str) -> str:
    return str(Path(path).expanduser()) if path.startswith("~") else path


def path_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    values = raw if isinstance(raw, (list, tuple)) else str(raw).split(", ")
    paths: list[str] = []
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        paths.append(text)
        expanded = expand_home(text)
        if expanded != text:
            paths.append(expanded)
    return paths


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def note_from_text(key: str, text: str, version: str, mtime: float, size: int) -> tuple[Note, str]:
    note = Note(key=key, version=version, mtime=mtime, size=size, folder=folder_of(key), stem=stem_of(key))
    try:
        frontmatter, body = parse(text)
    except FrontmatterError as exc:
        note.valid = False
        note.error = str(exc)
        note.title = note.stem
        return note, text
    note.title = _as_text(frontmatter.get("title")) or note.stem
    note.summary = _as_text(frontmatter.get("summary"))
    note.status = _as_text(frontmatter.get("status"))
    note.kind = _as_text(frontmatter.get("kind"))
    note.area = _as_text(frontmatter.get("area"))
    note.tags = tags_of(frontmatter)
    note.paths = path_list(frontmatter.get("path"))
    note.date = _as_text(frontmatter.get("date"))
    note.updated = _as_text(frontmatter.get("updated"))
    note.superseded_by = _as_text(frontmatter.get("superseded_by"))
    return note, body


def _row_to_note(row: sqlite3.Row) -> Note:
    return Note(
        key=row["key"],
        version=row["version"] or "",
        mtime=row["mtime"] or 0.0,
        folder=row["folder"] or "",
        stem=row["stem"] or "",
        title=row["title"] or "",
        summary=row["summary"] or "",
        status=row["status"] or "",
        kind=row["kind"] or "",
        area=row["area"] or "",
        tags=json.loads(row["tags"] or "[]"),
        paths=json.loads(row["paths"] or "[]"),
        date=row["date"] or "",
        updated=row["updated"] or "",
        superseded_by=row["superseded_by"] or "",
        valid=bool(row["valid"]),
        error=row["error"] or "",
        size=row["size"] or 0,
    )


class Index:
    def __init__(self, path: Path, backend: VaultBackend) -> None:
        self.path = Path(path)
        self.backend = backend
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript(SCHEMA_SQL)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
        return row["v"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, value))

    def remember_repo(self, repo: str, root: str) -> None:
        self.db.execute(
            "INSERT INTO repos(repo, root, seen_at) VALUES(?, ?, ?) "
            "ON CONFLICT(repo) DO UPDATE SET root = excluded.root, seen_at = excluded.seen_at",
            (repo, root, time.strftime("%Y-%m-%d")),
        )
        self.db.commit()

    def known_repos(self) -> list[tuple[str, str]]:
        rows = self.db.execute("SELECT repo, root FROM repos ORDER BY repo").fetchall()
        return [(row["repo"], row["root"]) for row in rows]

    def upsert(self, key: str, text: str, version: str, mtime: float, size: int) -> Note:
        note, body = note_from_text(key, text, version, mtime, size)
        self.remove(key)
        self.db.execute(
            f"INSERT INTO notes ({NOTE_COLUMNS}) VALUES ({', '.join('?' * 18)})",
            (
                note.key,
                note.version,
                note.mtime,
                note.folder,
                note.stem,
                note.title,
                note.summary,
                note.status,
                note.kind,
                note.area,
                json.dumps(note.tags),
                json.dumps(note.paths),
                note.date,
                note.updated,
                note.superseded_by,
                int(note.valid),
                note.error,
                note.size,
            ),
        )
        self.db.execute(
            "INSERT INTO notes_fts(title, summary, tags, body, key) VALUES(?, ?, ?, ?, ?)",
            (note.title, note.summary, " ".join(note.tags), body, note.key),
        )
        self.db.executemany("INSERT INTO shas(sha, key) VALUES(?, ?)", [(sha, key) for sha in shas_in(text)])
        self.db.executemany("INSERT INTO links(src, target) VALUES(?, ?)", [(key, t) for t in link_targets(text)])
        self.db.commit()
        return note

    def remove(self, key: str) -> None:
        self.db.execute("DELETE FROM notes WHERE key = ?", (key,))
        self.db.execute("DELETE FROM notes_fts WHERE key = ?", (key,))
        self.db.execute("DELETE FROM shas WHERE key = ?", (key,))
        self.db.execute("DELETE FROM links WHERE src = ?", (key,))
        self.db.commit()

    def note(self, key: str) -> Note | None:
        row = self.db.execute(f"SELECT {NOTE_COLUMNS} FROM notes WHERE key = ?", (key,)).fetchone()
        return _row_to_note(row) if row else None

    def all_notes(self) -> list[Note]:
        rows = self.db.execute(f"SELECT {NOTE_COLUMNS} FROM notes ORDER BY key").fetchall()
        return [_row_to_note(row) for row in rows]

    def stems(self) -> set[str]:
        return {row["stem"].lower() for row in self.db.execute("SELECT stem FROM notes")}

    def inbound_targets(self) -> set[str]:
        return {row["target"] for row in self.db.execute("SELECT DISTINCT target FROM links")}

    def keys_for_sha(self, prefix: str) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT key FROM shas WHERE sha LIKE ? || '%' OR ? LIKE sha || '%' ORDER BY key",
            (prefix, prefix),
        ).fetchall()
        return [row["key"] for row in rows]

    def _versions(self) -> dict[str, str]:
        return {row["key"]: row["version"] for row in self.db.execute("SELECT key, version FROM notes")}

    def _fetch(self, entries: list[Entry]) -> None:
        if not entries:
            return
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            fetched = list(pool.map(lambda entry: (entry, self._safe_get(entry.key)), entries))
        for entry, text in fetched:
            if text is None:
                continue
            self.upsert(entry.key, text, entry.version, entry.mtime.timestamp(), entry.size)

    def _safe_get(self, key: str) -> str | None:
        try:
            text, _ = self.backend.get(key)
        except Exception:
            return None
        return text

    def sync(self, force: bool = False) -> int:
        last = float(self.meta("last_sync") or 0.0)
        if not force and (time.time() - last) < SYNC_THROTTLE_SECONDS:
            return 0
        entries = [entry for entry in self.backend.list() if entry.key.endswith(".md")]
        known = self._versions()
        seen = {entry.key for entry in entries}
        changed = [entry for entry in entries if known.get(entry.key) != entry.version]
        for gone in known.keys() - seen:
            self.remove(gone)
        self._fetch(changed)
        self.set_meta("last_sync", str(time.time()))
        self.db.commit()
        return len(changed)

    def rebuild(self) -> int:
        for table in ("notes", "notes_fts", "shas", "links"):
            self.db.execute(f"DELETE FROM {table}")
        self.db.execute("DELETE FROM meta WHERE k = 'last_sync'")
        self.db.commit()
        return self.sync(force=True)
