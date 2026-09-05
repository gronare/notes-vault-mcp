from __future__ import annotations

import datetime as dt
import re
from typing import Any

import yaml

from notes_vault_mcp.schema import Schema

FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
AREA_RE = re.compile(r"^\[\[[^\]|]+(\|[^\]]+)?\]\]$")
PLAIN_SCALAR_RE = re.compile(r"^[^\s\"'\[\]{}#&*!|>%@`,][^:#\n]*$")
RESERVED_WORDS = frozenset({"true", "false", "yes", "no", "on", "off", "null", "~", ""})


class FrontmatterError(Exception):
    def __init__(self, message: str, line: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.line = line

    def __str__(self) -> str:
        return f"line {self.line}: {self.message}"


def parse(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return {}, text
    try:
        loaded = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = (mark.line + 2) if mark is not None else 1
        problem = getattr(exc, "problem", None) or "invalid YAML"
        raise FrontmatterError(problem, line) from exc
    if loaded is None:
        return {}, text[match.end() :]
    if not isinstance(loaded, dict):
        raise FrontmatterError("frontmatter is not a mapping", 2)
    return loaded, text[match.end() :]


def _scalar(value: Any) -> str:
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text.lower() in RESERVED_WORDS or not PLAIN_SCALAR_RE.match(text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def dump(frontmatter: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(_scalar(item) for item in value)}]")
        elif value is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.lstrip("\n")


def folder_of(key: str) -> str:
    return key.split("/")[0] if "/" in key else ""


def tags_of(frontmatter: dict[str, Any]) -> list[str]:
    raw = frontmatter.get("tags")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [part.strip() for part in raw.split(",") if part.strip()]
    return [str(item).strip() for item in raw if str(item).strip()]


def _missing(frontmatter: dict[str, Any], field: str) -> bool:
    value = frontmatter.get(field)
    return value is None or value == "" or value == []


def validate(frontmatter: dict[str, Any], key: str, schema: Schema) -> list[str]:
    problems: list[str] = []
    for field in schema.required_fields:
        if _missing(frontmatter, field):
            problems.append(f"missing required field: {field}")

    status = frontmatter.get("status")
    if status is not None and str(status) not in schema.status_values:
        problems.append(f"status '{status}' is not one of: {', '.join(schema.status_values)}")

    kind = frontmatter.get("kind")
    if kind is not None and str(kind) not in schema.kind_values:
        problems.append(f"kind '{kind}' is not one of: {', '.join(schema.kind_values)}")

    folder = folder_of(key)
    if folder in schema.area_required_in:
        area = frontmatter.get("area")
        if area is None or str(area).strip() == "":
            problems.append(f'missing area: notes in {folder}/ must link their system note as area: "[[stem]]"')
        elif not AREA_RE.match(str(area).strip()):
            problems.append(f"area '{area}' must be a wikilink shaped [[stem]]")

    if schema.tags_strict:
        vocabulary = set(schema.tag_vocabulary)
        for tag in tags_of(frontmatter):
            if tag not in vocabulary:
                problems.append(f"tag '{tag}' is not in the vocabulary")

    return problems
