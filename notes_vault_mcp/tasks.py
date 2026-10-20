from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from notes_vault_mcp import notes
from notes_vault_mcp.frontmatter import dump, folder_of, parse
from notes_vault_mcp.git import current_branch, in_main_checkout, main_checkout, repo_name
from notes_vault_mcp.index import Note, stem_of
from notes_vault_mcp.vault import Vault

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
DEFAULT_NEED = ("problem", "solution", "decisions")


@dataclass(frozen=True)
class Place:
    repo: str
    checkout: Path | None
    area: Note | None


def place(vault: Vault, cwd: str) -> Place:
    repo = repo_name(cwd)
    checkout = main_checkout(cwd)
    if checkout is not None:
        vault.index.remember_repo(repo, str(checkout))
    bundle = notes.context(vault, path=str(in_main_checkout(cwd)), repo=repo)
    return Place(repo=repo, checkout=checkout, area=bundle.system[0][0] if bundle.system else None)


def home_relative(path: Path) -> str:
    home = Path.home()
    return f"~/{path.relative_to(home)}" if path.is_relative_to(home) and path != home else str(path)


def task_stem(repo: str, name: str) -> str:
    family = notes.slugify(repo)
    slug = notes.slugify(name)
    if not slug:
        return family
    return slug if slug == family or slug.startswith(f"{family}-") else notes.slugify(f"{repo} {name}")


def free_key(vault: Vault, folder: str, stem: str) -> str:
    taken = {stem_of(entry.key).lower() for entry in vault.backend.list()}
    candidate, suffix = stem, 2
    while candidate.lower() in taken:
        candidate = f"{stem}-{suffix}"
        suffix += 1
    return f"{folder}/{candidate}.md"


def task_tags(vault: Vault, repo: str, area: Note | None) -> list[str]:
    vocabulary = vault.schema.tag_vocabulary
    if not vocabulary or repo in vocabulary:
        return [repo]
    known = [tag for tag in (area.tags if area else []) if tag in vocabulary]
    return known[:1] or [repo]


def section_skeleton(vault: Vault) -> str:
    return "\n\n".join(f"## {heading}" for heading in vault.schema.task_sections.values()) + "\n"


def open_task(vault: Vault, name: str, cwd: str, branch: str | None = None) -> dict:
    if not name.strip():
        raise ValueError("task open needs --name, or --note to claim a note")
    where = place(vault, cwd)
    area_stem = where.area.stem if where.area else None
    key = free_key(vault, vault.schema.task_folders[0], task_stem(where.repo, name))
    frontmatter: dict = {
        "title": name.strip(),
        "date": notes.today(),
        "updated": notes.today(),
        "tags": task_tags(vault, where.repo, where.area),
        "status": "active",
        "kind": "task",
        "area": f"[[{area_stem or where.repo}]]",
        "summary": "",
    }
    if where.checkout is not None:
        frontmatter["path"] = home_relative(where.checkout)
    on_branch = branch or current_branch(cwd)
    if on_branch:
        frontmatter["source"] = f"branch {on_branch}"
    notes.write(vault, key, dump(frontmatter, section_skeleton(vault)))
    return {"path": key, "created": True, "repo": where.repo, "area": area_stem}


def claim_task(vault: Vault, path: str, cwd: str) -> dict:
    if folder_of(path) not in vault.schema.task_folders:
        raise ValueError(f"{path} is not in a task folder ({', '.join(vault.schema.task_folders)})")
    text, _ = vault.backend.get(path)
    frontmatter, _ = parse(text)
    notes.set_status(vault, path, "active")
    area = notes._area_stem(str(frontmatter.get("area") or "")) or None
    return {"path": path, "created": False, "repo": repo_name(cwd), "area": area}


def filled_headings(body: str) -> dict[str, bool]:
    headings: list[tuple[int, str, int]] = []
    lines = body.splitlines()
    fenced = False
    for number, line in enumerate(lines):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip().casefold(), number))
    filled: dict[str, bool] = {}
    for position, (level, title, start) in enumerate(headings):
        end = next((line for lvl, _, line in headings[position + 1 :] if lvl <= level), len(lines))
        has_text = any(line.strip() for line in lines[start + 1 : end])
        filled[title] = filled.get(title, False) or has_text
    return filled


def missing_sections(vault: Vault, path: str, need: list[str]) -> list[str]:
    sections = vault.schema.task_sections
    unknown = [key for key in need if key not in sections]
    if unknown:
        raise ValueError(f"unknown section {', '.join(unknown)}; the schema names {', '.join(sections)}")
    text, _ = vault.backend.get(path)
    _, body = parse(text)
    filled = filled_headings(body)
    return [key for key in need if not filled.get(sections[key].strip().casefold(), False)]


def append_log(vault: Vault, cwd: str, line: str, commits: tuple[str, ...] = (), branch: str | None = None) -> dict:
    if not line.strip():
        raise ValueError("a log line needs text")
    where = place(vault, cwd)
    notes.log_append(
        vault,
        where.repo,
        line.strip(),
        commits=commits,
        area=where.area.stem if where.area else None,
        branch=branch or current_branch(cwd),
    )
    return {"path": vault.schema.log_key(where.repo)}
