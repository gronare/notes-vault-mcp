from __future__ import annotations

import re
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from notes_vault_mcp.vault import Vault, open_vault

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "vault"
TOKEN_RE = re.compile(r"@@(TODAY|D(\d+))@@")


def _substitute(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        days = int(match.group(2)) if match.group(2) else 0
        return (date.today() - timedelta(days=days)).isoformat()

    return TOKEN_RE.sub(replace, text)


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    destination = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, destination)
    for path in destination.rglob("*.md"):
        path.write_text(_substitute(path.read_text(encoding="utf-8")), encoding="utf-8")
    return destination


@pytest.fixture
def vault(vault_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Vault:
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("VAULT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.delenv("VAULT_SCHEMA", raising=False)
    opened = open_vault()
    opened.index.sync(force=True)
    yield opened
    opened.close()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
