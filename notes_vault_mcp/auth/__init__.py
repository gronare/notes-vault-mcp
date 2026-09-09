from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from notes_vault_mcp.config import ConfigError, cache_dir, env

AuthMode = Literal["bearer", "oidc", "builtin", "forwarded"]
READ_SCOPE = "vault:read"
WRITE_SCOPE = "vault:write"
SCOPES = (READ_SCOPE, WRITE_SCOPE)
DEFAULT_READ_GROUP = "vault"
DEFAULT_WRITE_GROUP = "vault-writers"
DEFAULT_IDP_SCOPES = ("openid", "profile", "email", "groups")
DEFAULT_IDENTITY_HEADER = "X-Forwarded-Identity"
DEFAULT_SUBJECT_PREFIX = "users"

NO_PUBLIC_URL = (
    "notes-vault-mcp: --auth {mode} needs VAULT_PUBLIC_URL, the https address clients reach this server on "
    "(for example https://home-vault.example.com); there is no default and never a fallback."
)
NO_ISSUER = "notes-vault-mcp: --auth oidc needs VAULT_OIDC_ISSUER, the identity provider's issuer URL."
NO_IDENTITY_KEY = (
    "notes-vault-mcp: --auth forwarded needs VAULT_IDENTITY_PUBLIC_KEY, the PEM public key (raw, or base64 on one "
    "line) the proxy signs its identity tokens with."
)
NO_IDENTITY_ISSUER = (
    "notes-vault-mcp: --auth forwarded needs VAULT_IDENTITY_ISSUER, the `iss` the proxy puts in its identity tokens."
)
BAD_IDENTITY_KEY = "notes-vault-mcp: VAULT_IDENTITY_PUBLIC_KEY is not a PEM public key: {reason}"


@dataclass(frozen=True)
class AuthConfig:
    mode: AuthMode
    public_url: str = ""
    bearer_token: str = ""
    issuer: str = ""
    audience: str = ""
    read_group: str = DEFAULT_READ_GROUP
    write_group: str = DEFAULT_WRITE_GROUP
    idp_scopes: tuple[str, ...] = DEFAULT_IDP_SCOPES
    auth_dir: Path | None = None
    identity_header: str = DEFAULT_IDENTITY_HEADER
    identity_public_key: str = ""
    identity_issuer: str = ""
    identity_typ: str = ""
    write_claims: tuple[str, ...] = (WRITE_SCOPE,)
    read_claims: tuple[str, ...] = (READ_SCOPE,)
    subject_prefix: str = DEFAULT_SUBJECT_PREFIX

    @property
    def path_prefix(self) -> str:
        return urlsplit(self.public_url).path.rstrip("/") if self.public_url else ""

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).hostname or ""

    @property
    def resource_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"

    @property
    def dav_url(self) -> str:
        return f"{self.public_url.rstrip('/')}/dav/"

    @property
    def advertised_scopes(self) -> tuple[str, ...]:
        return self.idp_scopes if self.mode == "oidc" else (READ_SCOPE,)


def auth_dir() -> Path:
    raw = env("VAULT_AUTH_DIR")
    path = Path(raw).expanduser() if raw else cache_dir() / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def public_key_pem(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("-----BEGIN"):
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise ConfigError(BAD_IDENTITY_KEY.format(reason="neither PEM nor base64 PEM")) from exc
    from cryptography.hazmat.primitives import serialization

    try:
        serialization.load_pem_public_key(text.encode("utf-8"))
    except ValueError as exc:
        raise ConfigError(BAD_IDENTITY_KEY.format(reason=exc)) from exc
    return text


def _words(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    return tuple((env(name) or "").split()) or default


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
            idp_scopes=_words("VAULT_OIDC_SCOPES", DEFAULT_IDP_SCOPES),
        )
    if mode == "forwarded":
        raw_key = env("VAULT_IDENTITY_PUBLIC_KEY")
        if not raw_key:
            raise ConfigError(NO_IDENTITY_KEY)
        issuer = env("VAULT_IDENTITY_ISSUER") or ""
        if not issuer:
            raise ConfigError(NO_IDENTITY_ISSUER)
        return AuthConfig(
            mode="forwarded",
            public_url=public_url,
            issuer=issuer,
            auth_dir=auth_dir(),
            identity_header=env("VAULT_IDENTITY_HEADER") or DEFAULT_IDENTITY_HEADER,
            identity_public_key=public_key_pem(raw_key),
            identity_issuer=issuer,
            identity_typ=env("VAULT_IDENTITY_TYP") or "",
            write_claims=_words("VAULT_IDENTITY_WRITE_CLAIMS", (WRITE_SCOPE,)),
            read_claims=_words("VAULT_IDENTITY_READ_CLAIMS", (READ_SCOPE,)),
            subject_prefix=(env("VAULT_SUBJECT_PREFIX") or DEFAULT_SUBJECT_PREFIX).strip("/"),
        )
    return AuthConfig(mode="builtin", public_url=public_url, issuer=public_url, auth_dir=auth_dir())
