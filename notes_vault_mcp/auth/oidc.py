from __future__ import annotations

import time
from typing import Any

import httpx2 as httpx
import jwt
from mcp.server.auth.provider import AccessToken

from notes_vault_mcp.auth import READ_SCOPE, SCOPES, WRITE_SCOPE, AuthConfig

ALGORITHMS = ["RS256", "ES256"]
DISCOVERY_PATH = "/.well-known/openid-configuration"
REFETCH_SECONDS = 60.0
TIMEOUT_SECONDS = 10.0


class OidcVerifier:
    def __init__(self, config: AuthConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._jwks_url = ""
        self._refetched_at: float | None = None

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = await self._claims(token)
        except Exception:
            return None
        if claims is None:
            return None
        scopes = self._scopes(claims)
        return self._access_token(token, claims, scopes) if scopes else None

    async def _claims(self, token: str) -> dict[str, Any] | None:
        key = await self._key(jwt.get_unverified_header(token).get("kid", ""))
        if key is None:
            return None
        return jwt.decode(
            token,
            key=key,
            algorithms=ALGORITHMS,
            issuer=self.config.issuer,
            audience=self.config.audience or None,
            options={"verify_aud": bool(self.config.audience), "require": ["exp", "iss"]},
        )

    async def _key(self, kid: str) -> jwt.PyJWK | None:
        if not self._keys:
            await self._load_keys()
        key = self._keys.get(kid)
        if key is None and self._may_refetch():
            self._refetched_at = time.monotonic()
            await self._load_keys()
            key = self._keys.get(kid)
        return key

    def _may_refetch(self) -> bool:
        return self._refetched_at is None or time.monotonic() - self._refetched_at >= REFETCH_SECONDS

    async def _load_keys(self) -> None:
        response = await self.client.get(await self._discover())
        response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json()).keys
        self._keys = {key.key_id: key for key in keys if key.key_id}

    async def _discover(self) -> str:
        if not self._jwks_url:
            response = await self.client.get(self.config.issuer + DISCOVERY_PATH)
            response.raise_for_status()
            self._jwks_url = str(response.json()["jwks_uri"])
        return self._jwks_url

    def _scopes(self, claims: dict[str, Any]) -> list[str]:
        groups = _groups(claims)
        if self.config.write_group in groups:
            return [READ_SCOPE, WRITE_SCOPE]
        if self.config.read_group in groups:
            return [READ_SCOPE]
        granted = str(claims.get("scope") or "").split()
        return [scope for scope in SCOPES if scope in granted]

    def _access_token(self, token: str, claims: dict[str, Any], scopes: list[str]) -> AccessToken:
        subject = claims.get("sub")
        return AccessToken(
            token=token,
            client_id=_client_id(claims),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.resource_url,
            subject=str(subject) if subject else None,
            claims=claims,
        )


def _groups(claims: dict[str, Any]) -> list[str]:
    raw = claims.get("groups") or []
    if isinstance(raw, str):
        return raw.split()
    return [str(item) for item in raw]


def _client_id(claims: dict[str, Any]) -> str:
    audience = claims.get("aud") or claims.get("azp") or ""
    if isinstance(audience, list):
        return str(audience[0]) if audience else ""
    return str(audience)
