from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from notes_vault_mcp.config import ConfigError, cache_dir, env

AuthMode = Literal["bearer", "oidc", "builtin"]
READ_SCOPE = "vault:read"
WRITE_SCOPE = "vault:write"
SCOPES = (READ_SCOPE, WRITE_SCOPE)
DEFAULT_READ_GROUP = "vault"
DEFAULT_WRITE_GROUP = "vault-writers"

NO_PUBLIC_URL = (
    "notes-vault-mcp: --auth {mode} needs VAULT_PUBLIC_URL, the https address clients reach this server on "
    "(for example https://home-vault.example.com); there is no default and never a fallback."
)
NO_ISSUER = "notes-vault-mcp: --auth oidc needs VAULT_OIDC_ISSUER, the identity provider's issuer URL."


@dataclass(frozen=True)
class AuthConfig:
    mode: AuthMode
    public_url: str = ""
    bearer_token: str = ""
    issuer: str = ""
    audience: str = ""
    read_group: str = DEFAULT_READ_GROUP
    write_group: str = DEFAULT_WRITE_GROUP
    auth_dir: Path | None = None

    @property
    def path_prefix(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/") if self.public_url else ""

    @property
    def resource_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"


def auth_dir() -> Path:
    raw = env("VAULT_AUTH_DIR")
    path = Path(raw).expanduser() if raw else cache_dir() / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def auth_config(mode: AuthMode) -> AuthConfig:
    if mode == "bearer":
        token = env("VAULT_TOKEN")
        if not token:
            raise ConfigError("notes-vault-mcp: --auth bearer needs VAULT_TOKEN set")
        return AuthConfig(mode="bearer", bearer_token=token)
    public_url = (env("VAULT_PUBLIC_URL") or "").rstrip("/")
    if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
        raise ConfigError(NO_PUBLIC_URL.format(mode=mode))
    if mode == "oidc":
        issuer = (env("VAULT_OIDC_ISSUER") or "").rstrip("/")
        if not issuer:
            raise ConfigError(NO_ISSUER)
        return AuthConfig(
            mode="oidc",
            public_url=public_url,
            issuer=issuer,
            audience=env("VAULT_OIDC_AUDIENCE") or "",
            read_group=env("VAULT_OIDC_READ_GROUP") or DEFAULT_READ_GROUP,
            write_group=env("VAULT_OIDC_WRITE_GROUP") or DEFAULT_WRITE_GROUP,
        )
    return AuthConfig(mode="builtin", public_url=public_url, issuer=public_url, auth_dir=auth_dir())
