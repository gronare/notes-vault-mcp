from __future__ import annotations

import hashlib
import os
from pathlib import Path

PLUGIN_ENV_PREFIX = "CLAUDE_PLUGIN_OPTION_"


class ConfigError(Exception):
    pass


def env(name: str, default: str | None = None) -> str | None:
    for key in (name, PLUGIN_ENV_PREFIX + name):
        value = os.environ.get(key)
        if value:
            return value
    return default


def cache_dir() -> Path:
    raw = env("VAULT_CACHE_DIR") or "~/.cache/notes-vault-mcp"
    path = Path(raw).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def vault_id(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]


def local_root() -> Path | None:
    raw = env("VAULT_PATH")
    return Path(raw).expanduser().resolve() if raw else None


def s3_settings() -> dict[str, str] | None:
    endpoint = env("S3_ENDPOINT")
    access_key = env("S3_ACCESS_KEY")
    secret_key = env("S3_SECRET_KEY")
    bucket = env("S3_BUCKET")
    if not (endpoint and access_key and secret_key and bucket):
        return None
    return {
        "endpoint": endpoint,
        "access_key": access_key,
        "secret_key": secret_key,
        "bucket": bucket,
        "prefix": env("S3_PREFIX", "") or "",
        "region": env("S3_REGION", "us-east-1") or "us-east-1",
    }


NO_BACKEND = (
    "notes-vault-mcp: no vault configured. Set VAULT_PATH to a directory, "
    "or S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY and S3_BUCKET for an S3 bucket."
)
