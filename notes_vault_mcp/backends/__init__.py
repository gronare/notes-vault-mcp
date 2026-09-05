from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

SCHEMA_KEY = ".vault/schema.yml"
NOTE_SUFFIXES = (".md", ".base")
SKIPPED_SEGMENTS = frozenset({".obsidian", ".trash"})


class VaultError(Exception):
    pass


class NotFound(VaultError):
    pass


class VersionConflict(VaultError):
    pass


@dataclass(frozen=True)
class Entry:
    key: str
    version: str
    mtime: datetime
    size: int


@runtime_checkable
class VaultBackend(Protocol):
    def list(self) -> list[Entry]: ...

    def get(self, key: str) -> tuple[str, str]: ...

    def put(self, key: str, text: str, expected_version: str | None = None) -> str: ...

    def delete(self, key: str) -> None: ...

    def move(self, src: str, dst: str) -> None: ...


def is_vault_key(key: str) -> bool:
    segments = key.split("/")
    if SKIPPED_SEGMENTS.intersection(segments):
        return False
    if key == SCHEMA_KEY:
        return True
    return key.endswith(NOTE_SUFFIXES)


def make_backend() -> tuple[VaultBackend, str]:
    from notes_vault_mcp.config import NO_BACKEND, ConfigError, local_root, s3_settings, vault_id

    root = local_root()
    if root is not None:
        from notes_vault_mcp.backends.local import LocalBackend

        return LocalBackend(root), vault_id(str(root))

    settings = s3_settings()
    if settings is not None:
        from notes_vault_mcp.backends.s3 import S3Backend

        seed = settings["endpoint"] + settings["bucket"] + settings["prefix"]
        return S3Backend(**settings), vault_id(seed)

    raise ConfigError(NO_BACKEND)
