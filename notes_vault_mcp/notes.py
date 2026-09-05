from __future__ import annotations

import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from notes_vault_mcp.backends import NotFound
from notes_vault_mcp.frontmatter import dump, parse, validate
from notes_vault_mcp.index import Note, expand_home, stem_of
from notes_vault_mcp.search import age_days, humanize_age
from notes_vault_mcp.vault import Vault

CONTEXT_TRUNCATE = 12000
TRUNCATED_MARKER = "… (truncated, read_file for the rest)"
LOG_TAIL_LINES = 20
OPEN_STATUSES = ("active", "draft")
BACKLOG_STATUS = "backlog"
PRIORITY_ORDER = ("urgent", "high", "medium", "low")
SLUG_MAX = 60


class ValidationError(Exception):
    pass


def today() -> str:
    return date.today().isoformat()


def _read(vault: Vault, path: str) -> tuple[str, str]:
    return vault.backend.get(path)


def _reindex(vault: Vault, path: str, text: str, version: str) -> None:
    vault.index.upsert(path, text, version, time.time(), len(text.encode("utf-8")))


def read(vault: Vault, path: str) -> str:
    text, version = _read(vault, path)
    frontmatter, body = parse(text)
    superseded_by = frontmatter.get("superseded_by")
    if superseded_by:
        body = f"> [!warning] Superseded by {superseded_by}\n\n{body.lstrip()}"
        text = dump(frontmatter, body)
    return f"etag: {version}\n{text}"


def _with_dates(frontmatter: dict) -> dict:
    stamped = dict(frontmatter)
    stamped["updated"] = today()
    if not stamped.get("date"):
        ordered: dict = {}
        for key, value in stamped.items():
            ordered[key] = value
            if key == "title":
                ordered["date"] = today()
        ordered.setdefault("date", today())
        stamped = ordered
    return stamped


def write(vault: Vault, path: str, content: str, expected_etag: str | None = None) -> str:
    frontmatter, body = parse(content)
    stamped = _with_dates(frontmatter)
    problems = validate(stamped, path, vault.schema)
    if problems:
        raise ValidationError("\n".join([f"{path} is not a valid note:", *[f"- {p}" for p in problems]]))
    text = dump(stamped, body)
    version = vault.backend.put(path, text, expected_version=expected_etag)
    _reindex(vault, path, text, version)
    return f"Written: {path}\netag: {version}"


def append(vault: Vault, path: str, content: str) -> str:
    try:
        existing, _ = _read(vault, path)
    except NotFound:
        existing = ""
    frontmatter, body = parse(existing)
    body = f"{body.rstrip()}\n{content}\n" if body.strip() else f"{content}\n"
    text = dump(_with_dates(frontmatter), body) if frontmatter else body
    version = vault.backend.put(path, text)
    _reindex(vault, path, text, version)
    return f"Appended to: {path}"


def move(vault: Vault, src: str, dst: str) -> str:
    vault.backend.move(src, dst)
    text, version = _read(vault, dst)
    vault.index.remove(src)
    _reindex(vault, dst, text, version)
    return f"Moved: {src} -> {dst}"


def delete(vault: Vault, path: str) -> str:
    vault.backend.delete(path)
    vault.index.remove(path)
    return f"Deleted: {path}"


def _free_key(vault: Vault, folder: str, basename: str) -> str:
    stem = basename.removesuffix(".md")
    candidate = f"{folder}/{stem}.md"
    suffix = 2
    taken = {entry.key for entry in vault.backend.list()}
    while candidate in taken:
        candidate = f"{folder}/{stem}-{suffix}.md"
        suffix += 1
    return candidate


def close(vault: Vault, path: str, merged_into: str | None = None, status: str | None = None) -> str:
    text, _ = _read(vault, path)
    frontmatter, body = parse(text)
    if merged_into:
        frontmatter["status"] = "superseded"
        frontmatter["superseded_by"] = f"[[{stem_of(merged_into)}]]"
    else:
        frontmatter["status"] = status or "complete"
    frontmatter["updated"] = today()
    destination = _free_key(vault, vault.schema.archive_folder, path.rsplit("/", 1)[-1])
    version = vault.backend.put(destination, dump(frontmatter, body))
    vault.backend.delete(path)
    vault.index.remove(path)
    _reindex(vault, destination, dump(frontmatter, body), version)
    return destination


def _area_link(area: str | None, repo: str) -> str:
    stem = (area or repo).strip()
    return stem if stem.startswith("[[") else f"[[{stem}]]"


def _new_log(repo: str, area: str | None) -> tuple[dict, str]:
    frontmatter = {
        "title": f"Logg — {repo}",
        "date": today(),
        "updated": today(),
        "tags": ["log", repo],
        "status": "active",
        "kind": "log",
        "area": _area_link(area, repo),
    }
    return frontmatter, ""


def log_append(vault: Vault, repo: str, line: str, commits: tuple[str, ...] = (), area: str | None = None) -> str:
    key = vault.schema.log_key(repo)
    try:
        existing, _ = _read(vault, key)
        frontmatter, body = parse(existing)
    except NotFound:
        frontmatter, body = _new_log(repo, area)
    if area:
        frontmatter["area"] = _area_link(area, repo)
    frontmatter["updated"] = today()
    entry = vault.schema.log_entry_format.format(
        date=today(),
        line=line,
        commits=" ".join(commits) if commits else "-",
        area=frontmatter.get("area") or "-",
    )
    body = f"{body.rstrip()}\n{entry}\n" if body.strip() else f"{entry}\n"
    text = dump(frontmatter, body)
    version = vault.backend.put(key, text)
    _reindex(vault, key, text, version)
    return f"Logged to {key}:\n{entry}"


def priority_rank(value: str) -> int:
    return PRIORITY_ORDER.index(value) if value in PRIORITY_ORDER else len(PRIORITY_ORDER)


def slugify(title: str) -> str:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", folded).strip("-")
    return slug[:SLUG_MAX].rstrip("-")


def _area_stem(area: str) -> str:
    return stem_of(area.strip().strip("[]").split("|")[0].strip())


def backlog_add(
    vault: Vault,
    title: str,
    area: str,
    line: str,
    priority: str | None = None,
    source: str | None = None,
) -> str:
    area_stem = _area_stem(area)
    if not area_stem:
        raise ValueError("area is required: the stem of the system note, or [[stem]]")
    family = area_stem.split("-")[0]
    slug = slugify(title)
    stem = slug if slug == family or slug.startswith(f"{family}-") else f"{family}-{slug}"
    key = _free_key(vault, vault.schema.task_folders[0], stem)
    frontmatter: dict = {
        "title": title,
        "date": today(),
        "updated": today(),
        "tags": [family],
        "status": BACKLOG_STATUS,
        "kind": "task",
        "area": f"[[{area_stem}]]",
        "summary": line,
    }
    if priority:
        frontmatter["priority"] = priority
    if source:
        frontmatter["source"] = source
    problems = validate(frontmatter, key, vault.schema)
    if problems:
        raise ValueError("; ".join(problems))
    text = dump(frontmatter, f"{line}\n")
    _reindex(vault, key, text, vault.backend.put(key, text))
    return key


def _in_area(note: Note, area: str | None) -> bool:
    if not area:
        return True
    wanted = _area_stem(area)
    have = _area_stem(note.area)
    return have == wanted or have.startswith(f"{wanted}-")


def backlog(vault: Vault, area: str | None = None, priority: str | None = None) -> list[Note]:
    task_folders = set(vault.schema.task_folders)
    rows = [
        note
        for note in vault.index.all_notes()
        if note.valid
        and note.folder in task_folders
        and note.status == BACKLOG_STATUS
        and _in_area(note, area)
        and (not priority or note.priority == priority)
    ]
    rows.sort(key=lambda note: note.updated, reverse=True)
    rows.sort(key=lambda note: priority_rank(note.priority))
    return rows


def render_backlog(rows: list[Note]) -> str:
    if not rows:
        return "The backlog is empty."
    now = time.time()
    lines = [
        f"- {note.key} | {note.title} | {note.priority or '-'} | {note.area or '-'} | "
        f"{humanize_age(age_days(note, now))}"
        for note in rows
    ]
    return f"{len(rows)} backlog items\n" + "\n".join(lines)


def path_overlap(note: Note, target: str | None) -> int:
    if not target:
        return 0
    wanted = expand_home(target).rstrip("/")
    best = 0
    for raw in note.paths:
        candidate = expand_home(raw).rstrip("/")
        if not candidate:
            continue
        if wanted.startswith(candidate) or candidate.startswith(wanted):
            best = max(best, len(os.path.commonprefix([wanted, candidate])))
    return best


def mentions_repo(note: Note, repo: str | None) -> bool:
    if not repo:
        return False
    needle = repo.lower()
    haystack = [note.title.lower(), note.stem.lower(), *[tag.lower() for tag in note.tags]]
    return any(needle in item for item in haystack)


def _relevant(note: Note, path: str | None, repo: str | None) -> bool:
    return path_overlap(note, path) > 0 or mentions_repo(note, repo)


@dataclass
class ContextBundle:
    system: list[tuple[Note, str]] = field(default_factory=list)
    system_rows: list[Note] = field(default_factory=list)
    tasks: list[Note] = field(default_factory=list)
    backlog: list[Note] = field(default_factory=list)
    backlog_total: int = 0
    references: list[Note] = field(default_factory=list)
    log_tail: list[str] = field(default_factory=list)
    repo: str | None = None
    stale_after_days: int = 30

    def render(self) -> str:
        now = time.time()
        blocks = []
        if self.system:
            blocks.append("## system notes\n" + "\n\n".join(f"### {note.key}\n{text}" for note, text in self.system))
        if self.system_rows:
            rows = [
                f"{note.key} | {note.title} | {humanize_age(age_days(note, now))} | {note.status}"
                for note in self.system_rows
            ]
            blocks.append("## other system notes\n" + "\n".join(rows))
        if self.tasks:
            rows = [_task_row(note, now, self.stale_after_days) for note in self.tasks]
            blocks.append("## open tasks\n" + "\n".join(rows))
        if self.backlog:
            rows = [f"{note.key} | {note.title} | {note.priority or '-'}" for note in self.backlog]
            blocks.append(f"## backlog ({self.backlog_total})\n" + "\n".join(rows))
        if self.references:
            rows = [f"{note.key} | {note.title} | {humanize_age(age_days(note, now))}" for note in self.references]
            blocks.append("## reference notes\n" + "\n".join(rows))
        if self.log_tail:
            blocks.append(f"## log — {self.repo}\n" + "\n".join(self.log_tail))
        if not blocks:
            return "No vault context for this path or repo."
        return "\n\n".join(blocks)


def _task_row(note: Note, now: float, stale_after_days: int) -> str:
    days = age_days(note, now)
    stale = " STALE" if days > stale_after_days else ""
    return f"{note.key} | {note.title} | {humanize_age(days)} | {note.status}{stale}"


def _truncate(text: str) -> str:
    if len(text) <= CONTEXT_TRUNCATE:
        return text
    return text[:CONTEXT_TRUNCATE] + "\n" + TRUNCATED_MARKER


def _system_rank(note: Note, path: str | None, repo: str | None) -> tuple:
    target = expand_home(path).rstrip("/") if path else ""
    exact = any(expand_home(p).rstrip("/") == target for p in note.paths) if target else False
    return (not exact, note.stem != repo, note.status != "active", -path_overlap(note, path), note.key)


def _system_notes(
    vault: Vault, notes: list[Note], path: str | None, repo: str | None
) -> tuple[list[tuple[Note, str]], list[Note]]:
    folders = set(vault.schema.system_folders)
    candidates = [n for n in notes if (n.kind == "system" or n.folder in folders) and _relevant(n, path, repo)]
    candidates.sort(key=lambda note: _system_rank(note, path, repo))
    hub = next((n for n in candidates if n.stem == repo), None)
    floor = path_overlap(hub, path) if hub else -1
    full = ([hub] if hub else []) + [n for n in candidates if n is not hub and path_overlap(n, path) > floor]
    full = full[:3]
    loaded = []
    for note in full:
        try:
            text, _ = _read(vault, note.key)
        except Exception:
            continue
        loaded.append((note, _truncate(text)))
    rows = [n for n in candidates if n not in full][:15]
    return loaded, rows


def _log_tail(vault: Vault, repo: str | None) -> list[str]:
    if not repo:
        return []
    try:
        text, _ = _read(vault, vault.schema.log_key(repo))
    except Exception:
        return []
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-LOG_TAIL_LINES:]


def context(
    vault: Vault,
    path: str | None = None,
    repo: str | None = None,
    query: str | None = None,
    limit: int = 10,
) -> ContextBundle:
    notes = [note for note in vault.index.all_notes() if note.valid]
    task_folders = set(vault.schema.task_folders)
    reference_folders = set(vault.schema.reference_folders)
    needle = (query or "").lower()

    tasks = [
        note
        for note in notes
        if note.folder in task_folders and note.status in OPEN_STATUSES and _relevant(note, path, repo)
    ]
    references = [
        note
        for note in notes
        if note.folder in reference_folders
        and (_relevant(note, path, repo) or (needle and needle in f"{note.title} {' '.join(note.tags)}".lower()))
    ]
    backlog_rows = [
        note
        for note in notes
        if note.folder in task_folders and note.status == BACKLOG_STATUS and _relevant(note, path, repo)
    ]
    tasks.sort(key=lambda note: note.updated, reverse=True)
    references.sort(key=lambda note: note.updated, reverse=True)
    backlog_rows.sort(key=lambda note: note.updated, reverse=True)
    backlog_rows.sort(key=lambda note: priority_rank(note.priority))
    system, system_rows = _system_notes(vault, notes, path, repo)
    return ContextBundle(
        system=system,
        system_rows=system_rows,
        tasks=tasks[:limit],
        backlog=backlog_rows[:limit],
        backlog_total=len(backlog_rows),
        references=references[:limit],
        log_tail=_log_tail(vault, repo),
        repo=repo,
        stale_after_days=vault.schema.stale_after_days,
    )


@dataclass
class Findings:
    groups: dict[str, list[str]] = field(default_factory=dict)

    def add(self, kind: str, entry: str) -> None:
        self.groups.setdefault(kind, []).append(entry)

    def as_dict(self) -> dict[str, list[str]]:
        return {kind: sorted(entries) for kind, entries in sorted(self.groups.items()) if entries}

    def render(self) -> str:
        grouped = self.as_dict()
        if not grouped:
            return "No findings."
        total = sum(len(entries) for entries in grouped.values())
        blocks = [f"{total} findings"]
        for kind, entries in grouped.items():
            blocks.append(f"\n## {kind} ({len(entries)})\n" + "\n".join(f"- {entry}" for entry in entries))
        return "\n".join(blocks)


def _lint_frontmatter(vault: Vault, note: Note, findings: Findings) -> None:
    if not note.valid:
        findings.add("broken_frontmatter", f"{note.key}: {note.error}")
        return
    try:
        text, _ = _read(vault, note.key)
        frontmatter, _ = parse(text)
    except Exception as exc:
        findings.add("broken_frontmatter", f"{note.key}: {exc}")
        return
    for problem in validate(frontmatter, note.key, vault.schema):
        if problem.startswith("missing required field: "):
            findings.add("missing_required", f"{note.key}: {problem.removeprefix('missing required field: ')}")
        elif problem.startswith("missing area") or problem.startswith("area "):
            findings.add("missing_area", f"{note.key}: {problem}")
        elif problem.startswith("tag "):
            findings.add("unknown_tags", f"{note.key}: {problem}")
        else:
            findings.add("invalid_field", f"{note.key}: {problem}")


def _lint_links(vault: Vault, notes: list[Note], findings: Findings) -> None:
    stems = vault.index.stems()
    rows = vault.index.db.execute("SELECT src, target FROM links ORDER BY src, target").fetchall()
    for row in rows:
        if row["target"] not in stems:
            findings.add("unresolved_links", f"{row['src']} -> [[{row['target']}]]")
    linked = vault.index.inbound_targets()
    ignored = {vault.schema.log_folder, vault.schema.archive_folder}
    for note in notes:
        if note.folder in ignored or note.area:
            continue
        if note.stem.lower() not in linked:
            findings.add("orphans", note.key)


def _lint_lifecycle(vault: Vault, notes: list[Note], findings: Findings) -> None:
    now = time.time()
    task_folders = set(vault.schema.task_folders)
    archive = vault.schema.archive_folder
    stems = vault.index.stems()
    log = vault.schema.log_folder
    by_stem: dict[str, list[str]] = {}
    for note in notes:
        if note.folder != log:
            by_stem.setdefault(note.stem.lower(), []).append(note.key)
        if note.folder in task_folders and note.status in OPEN_STATUSES:
            days = age_days(note, now)
            if days > vault.schema.stale_after_days:
                findings.add("stale_active", f"{note.key}: {humanize_age(days)} old, status {note.status}")
        if note.folder in task_folders and note.status in ("complete", "superseded"):
            findings.add("done_not_closed", f"{note.key}: status {note.status}, still in {note.folder}/")
        if note.folder == archive and note.status not in ("complete", "superseded"):
            findings.add("archive_status_mismatch", f"{note.key}: status {note.status or '-'}")
        if note.superseded_by:
            target = note.superseded_by.strip("[]").split("|")[0].strip().lower()
            if target and target not in stems:
                findings.add("superseded_target_missing", f"{note.key} -> {note.superseded_by}")
    for stem, keys in by_stem.items():
        if len(keys) > 1:
            findings.add("duplicate_stems", f"{stem}: {', '.join(sorted(keys))}")


def lint(vault: Vault) -> Findings:
    findings = Findings()
    notes = vault.index.all_notes()
    for note in notes:
        _lint_frontmatter(vault, note, findings)
    _lint_links(vault, [note for note in notes if note.valid], findings)
    _lint_lifecycle(vault, [note for note in notes if note.valid], findings)
    return findings


def lint_note_text(findings: Findings) -> str:
    frontmatter = {
        "title": f"Lint {today()}",
        "date": today(),
        "updated": today(),
        "tags": ["log"],
        "status": "complete",
        "kind": "log",
        "area": "[[vault]]",
    }
    return dump(frontmatter, findings.render())


def changelog_notes(vault: Vault, period: str) -> list[Note]:
    return [note for note in vault.index.all_notes() if note.date.startswith(period)]


def log_lines(vault: Vault, repo: str, period: str) -> list[str]:
    try:
        text, _ = _read(vault, vault.schema.log_key(repo))
    except Exception:
        return []
    return [line for line in text.splitlines() if line.lstrip().startswith(f"- [{period}")]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def vault_paths_in(note: Note, root: Path) -> list[str]:
    target = _resolved(root)
    inside = []
    for raw in note.paths:
        candidate = _resolved(Path(expand_home(raw)))
        if candidate == target or target in candidate.parents:
            inside.append(str(candidate))
    return inside
