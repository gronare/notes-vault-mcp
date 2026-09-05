from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

from notes_vault_mcp.backends import Entry, NotFound, VersionConflict, is_vault_key


def _version(stat: os.stat_result) -> str:
    return f"{stat.st_mtime_ns}:{stat.st_size}"


class LocalBackend:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise NotFound(f"outside the vault: {key}")
        return path

    def list(self) -> list[Entry]:
        entries: list[Entry] = []
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self.root).as_posix()
            if not is_vault_key(key):
                continue
            stat = path.stat()
            entries.append(
                Entry(
                    key=key,
                    version=_version(stat),
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    size=stat.st_size,
                )
            )
        return sorted(entries, key=lambda entry: entry.key)

    def get(self, key: str) -> tuple[str, str]:
        path = self._path(key)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise NotFound(key) from exc
        return text, _version(path.stat())

    def current_version(self, key: str) -> str | None:
        path = self._path(key)
        return _version(path.stat()) if path.exists() else None

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        path = self._path(key)
        if expected_version is not None:
            current = self.current_version(key)
            if current != expected_version:
                raise VersionConflict(f"{key} changed since it was read (etag {current}, expected {expected_version})")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return _version(path.stat())

    def delete(self, key: str) -> None:
        path = self._path(key)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise NotFound(key) from exc

    def move(self, src: str, dst: str) -> None:
        source = self._path(src)
        target = self._path(dst)
        if not source.exists():
            raise NotFound(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.replace(target)
