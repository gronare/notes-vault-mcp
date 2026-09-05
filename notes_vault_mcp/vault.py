from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from notes_vault_mcp.backends import VaultBackend, make_backend
from notes_vault_mcp.config import cache_dir, env
from notes_vault_mcp.index import Index
from notes_vault_mcp.schema import Schema, load_schema


@dataclass
class Vault:
    backend: VaultBackend
    index: Index
    schema: Schema

    def close(self) -> None:
        self.index.close()


def open_vault(cache: Path | None = None) -> Vault:
    backend, identifier = make_backend()
    schema = load_schema(backend, env("VAULT_SCHEMA"))
    index = Index((cache or cache_dir()) / f"{identifier}.sqlite", backend)
    return Vault(backend=backend, index=index, schema=schema)
