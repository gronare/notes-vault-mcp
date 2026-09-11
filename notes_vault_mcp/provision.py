from __future__ import annotations

from datetime import date
from importlib import resources

import yaml

from notes_vault_mcp.backends import SCHEMA_KEY, VaultBackend

BASES = ("Areas.base", "Open tasks.base", "Resources.base", "Backlog.base", "Log.base")
TEMPLATES = ("schema.yml", *BASES)
WELCOME_KEY = "Welcome.md"


def template(name: str) -> str:
    return resources.files("notes_vault_mcp.templates").joinpath(name).read_text(encoding="utf-8")


def target_key(name: str) -> str:
    return SCHEMA_KEY if name.startswith("schema") else name


def _fill(text: str, values: dict[str, str]) -> str:
    for name, value in values.items():
        text = text.replace("{{" + name + "}}", value)
    return text


def initialize(
    backend: VaultBackend,
    force: bool = False,
    schema_template: str = "schema.yml",
    welcome: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    existing = {entry.key for entry in backend.list()}
    written: list[str] = []
    kept: list[str] = []
    schema_text = template(schema_template)
    stale = str((yaml.safe_load(schema_text) or {}).get("stale_after_days", 30))
    plan = [(SCHEMA_KEY, schema_text)] + [(name, _fill(template(name), {"STALE_DAYS": stale})) for name in BASES]
    if welcome is not None:
        values = {"DATE": date.today().isoformat(), **welcome}
        plan.append((WELCOME_KEY, _fill(template("welcome.md"), values)))
    for key, text in plan:
        if key in existing and not force:
            kept.append(key)
            continue
        backend.put(key, text)
        written.append(key)
    return written, kept


def is_empty(backend: VaultBackend) -> bool:
    return not backend.list()
