from __future__ import annotations

import hashlib
import json
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


PLUGIN_NAME = "vault"
PLUGIN_OPTIONS = {
    "vault_path": "VAULT_PATH",
    "s3_endpoint": "S3_ENDPOINT",
    "s3_bucket": "S3_BUCKET",
    "s3_access_key": "S3_ACCESS_KEY",
    "s3_secret_key": "S3_SECRET_KEY",
    "s3_prefix": "S3_PREFIX",
    "s3_region": "S3_REGION",
}


def claude_settings_path() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser() / "settings.json"


def plugin_options(settings_path: Path | None = None) -> dict[str, str]:
    """The vault plugin's user config as Claude Code saved it, keyed by the variable each option feeds."""
    try:
        data = json.loads((settings_path or claude_settings_path()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    configs = data.get("pluginConfigs") if isinstance(data, dict) else None
    for plugin, config in (configs or {}).items():
        if plugin.split("@", 1)[0] != PLUGIN_NAME or not isinstance(config, dict):
            continue
        options = config.get("options") or {}
        found = {
            PLUGIN_OPTIONS[name]: value.strip()
            for name, value in options.items()
            if name in PLUGIN_OPTIONS and isinstance(value, str) and value.strip()
        }
        if found:
            return found
    return {}


def adopt_plugin_config() -> str:
    """Fill the storage variables from the vault plugin's saved config when the environment names no vault."""
    if local_root() is not None or s3_settings() is not None:
        return "env"
    found = plugin_options()
    for name, value in found.items():
        if not env(name):
            os.environ[name] = value
    return "plugin" if found else "none"
