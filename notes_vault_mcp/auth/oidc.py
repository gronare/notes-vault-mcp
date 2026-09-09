from __future__ import annotations

import hashlib
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
USERINFO_TTL = 300.0


class OidcVerifier:
    def __init__(self, config: AuthConfig, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self.client = client or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        self._keys: dict[str, jwt.PyJWK] = {}
        self._document: dict[str, Any] = {}
        self._refetched_at: float | None = None
        self._userinfo: dict[str, tuple[float, dict[str, Any]]] = {}

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            return await self._verified(token)
        except Exception:
            return None

    async def _verified(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            return await self._from_userinfo(token)
        claims = await self._claims(token, header.get("kid", ""))
        if claims is None:
            return None
        scopes = await self._scopes(token, claims)
        return self._access_token(token, claims, scopes) if scopes else None

    async def _claims(self, token: str, kid: str) -> dict[str, Any] | None:
        key = await self._key(kid)
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
        response = await self.client.get(await self._endpoint("jwks_uri"))
        response.raise_for_status()
        keys = jwt.PyJWKSet.from_dict(response.json()).keys
        self._keys = {key.key_id: key for key in keys if key.key_id}

    async def _endpoint(self, name: str) -> str:
        if not self._document:
            response = await self.client.get(self.config.issuer + DISCOVERY_PATH)
            response.raise_for_status()
            self._document = response.json()
        return str(self._document[name])

    async def _scopes(self, token: str, claims: dict[str, Any]) -> list[str]:
        if "groups" in claims or _granted(claims):
            return self._decide(claims)
        return self._decide(await self._userinfo_claims(token, _expiry(claims)))

    def _decide(self, claims: dict[str, Any]) -> list[str]:
        groups = _groups(claims)
        if self.config.write_group in groups:
            return [READ_SCOPE, WRITE_SCOPE]
        if self.config.read_group in groups:
            return [READ_SCOPE]
        return _granted(claims)

    async def _userinfo_claims(self, token: str, expires_at: float) -> dict[str, Any]:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()
        cached = self._userinfo.get(digest)
        if cached is not None and cached[0] > now:
            return cached[1]
        response = await self.client.get(
            await self._endpoint("userinfo_endpoint"), headers={"Authorization": f"Bearer {token}"}
        )
        response.raise_for_status()
        claims = response.json()
        self._userinfo = {key: entry for key, entry in self._userinfo.items() if entry[0] > now}
        self._userinfo[digest] = (expires_at, claims)
        return claims

    async def _from_userinfo(self, token: str) -> AccessToken | None:
        claims = await self._userinfo_claims(token, time.time() + USERINFO_TTL)
        subject = claims.get("sub")
        scopes = self._decide(claims)
        if not subject or not scopes:
            return None
        return AccessToken(
            token=token,
            client_id=_client_id(claims),
            scopes=scopes,
            expires_at=None,
            resource=self.config.resource_url,
            subject=str(subject),
            claims=claims,
        )

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


def _expiry(claims: dict[str, Any]) -> float:
    return min(float(claims["exp"]), time.time() + USERINFO_TTL)


def _granted(claims: dict[str, Any]) -> list[str]:
    named = str(claims.get("scope") or "").split()
    return [scope for scope in SCOPES if scope in named]


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
