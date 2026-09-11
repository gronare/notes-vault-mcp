from __future__ import annotations

import hashlib
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import yaml

from notes_vault_mcp import provision
from notes_vault_mcp.backends import VaultBackend, VaultError, make_backend, make_subject_backend
from notes_vault_mcp.config import cache_dir, env
from notes_vault_mcp.index import Index
from notes_vault_mcp.schema import Schema, deep_merge, default_schema_data, load_schema

SUBJECT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@-]{0,63}")
IDLE_SECONDS = 1800.0


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


class NoSubject(VaultError):
    pass


class VaultResolver(Protocol):
    def current(self) -> Vault: ...

    @property
    def instruction_schema(self) -> Schema: ...

    def close(self) -> None: ...


class SingleVault:
    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    def current(self) -> Vault:
        return self.vault

    @property
    def instruction_schema(self) -> Schema:
        return self.vault.schema

    def close(self) -> None:
        self.vault.close()


def subject_key(subject: str) -> str:
    # The subject names a folder, so anything outside a plain identifier is hashed rather than trusted as a path.
    return subject if SUBJECT_RE.fullmatch(subject) else hashlib.sha1(subject.encode("utf-8")).hexdigest()[:16]


class SubjectVaults:
    # One vault per authenticated subject under a common prefix, provisioned on first use and closed when idle.
    def __init__(
        self,
        cache: Path | None = None,
        prefix: str = "users",
        dav_url: str = "",
        schema_template: str = "schema.yml",
        idle_seconds: float = IDLE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cache = cache or cache_dir()
        self.prefix = prefix.strip("/")
        self.dav_url = dav_url
        self.schema_template = schema_template
        self.idle_seconds = idle_seconds
        self.clock = clock
        self._open: dict[str, tuple[Vault, float]] = {}
        self._lock = threading.Lock()
        self._instruction_schema: Schema | None = None

    def current(self) -> Vault:
        from mcp.server.auth.middleware.auth_context import get_access_token

        token = get_access_token()
        if token is None or not token.subject:
            raise NoSubject("no authenticated subject on this request")
        return self.for_subject(token.subject)

    def for_subject(self, subject: str) -> Vault:
        key = subject_key(subject)
        with self._lock:
            self._evict()
            opened = self._open.get(key)
            vault = opened[0] if opened else self._open_vault(key)
            self._open[key] = (vault, self.clock())
            return vault

    def _open_vault(self, key: str) -> Vault:
        backend, identifier = make_subject_backend(key, self.prefix)
        if provision.is_empty(backend):
            provision.initialize(backend, schema_template=self.schema_template, welcome={"DAV_URL": self.dav_url})
        return Vault(backend=backend, schema=load_schema(backend), index_path=self.cache / f"{identifier}.sqlite")

    def _evict(self) -> None:
        now = self.clock()
        for key, (vault, last_used) in list(self._open.items()):
            if now - last_used > self.idle_seconds:
                vault.close()
                del self._open[key]

    @property
    def open_subjects(self) -> list[str]:
        return sorted(self._open)

    @property
    def instruction_schema(self) -> Schema:
        if self._instruction_schema is None:
            override = yaml.safe_load(provision.template(self.schema_template)) or {}
            self._instruction_schema = Schema(deep_merge(default_schema_data(), override))
        return self._instruction_schema

    def close(self) -> None:
        with self._lock:
            for vault, _ in self._open.values():
                vault.close()
            self._open.clear()
