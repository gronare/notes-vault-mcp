from __future__ import annotations

from pathlib import Path

from notes_vault_mcp.backends import VaultBackend, make_backend
from notes_vault_mcp.config import cache_dir, env
from notes_vault_mcp.index import Index
from notes_vault_mcp.schema import Schema, load_schema


class Vault:
    def __init__(
        self, backend: VaultBackend, schema: Schema, index_path: Path | None = None, index: Index | None = None
    ):
        self.backend = backend
        self.schema = schema
        self.index_path = index_path
        self._index = index

    @property
    def index(self) -> Index:
        if self._index is None:
            if self.index_path is None:
                raise RuntimeError("the vault has no index path")
            self._index = Index(self.index_path, self.backend)
        return self._index

    @property
    def index_opened(self) -> bool:
        return self._index is not None

    def close(self) -> None:
        if self._index is not None:
            self._index.close()
            self._index = None


def open_vault(cache: Path | None = None) -> Vault:
    backend, identifier = make_backend()
    schema = load_schema(backend, env("VAULT_SCHEMA"))
    return Vault(backend=backend, schema=schema, index_path=(cache or cache_dir()) / f"{identifier}.sqlite")
