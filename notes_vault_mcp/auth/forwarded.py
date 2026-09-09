from __future__ import annotations

import json
from typing import Any

import jwt
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from starlette.authentication import AuthCredentials
from starlette.types import ASGIApp, Receive, Scope, Send

from notes_vault_mcp.auth import READ_SCOPE, WRITE_SCOPE, AuthConfig

ALGORITHMS = ["RS256", "ES256"]


class IdentityError(Exception):
    def __init__(self, error: str, description: str) -> None:
        super().__init__(description)
        self.error = error
        self.description = description


def _entitlements(claims: dict[str, Any]) -> set[str]:
    raw = claims.get("entitlements") or []
    named = set(raw.split()) if isinstance(raw, str) else {str(item) for item in raw}
    named.update(str(claims.get("scope") or "").split())
    return named


class ForwardedVerifier:
    def __init__(self, config: AuthConfig) -> None:
        self.config = config

    def verify(self, token: str) -> AccessToken:
        try:
            claims = jwt.decode(
                token,
                key=self.config.identity_public_key,
                algorithms=ALGORITHMS,
                issuer=self.config.identity_issuer,
                audience=self.config.public_host,
                options={"require": ["exp", "sub", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise IdentityError("invalid_token", str(exc)) from exc
        if self.config.identity_typ and claims.get("typ") != self.config.identity_typ:
            raise IdentityError("invalid_token", "wrong token type")
        scopes = self.scopes(claims)
        if not scopes:
            raise IdentityError("insufficient_scope", "no vault entitlement in the forwarded identity")
        audience = claims.get("aud")
        return AccessToken(
            token=token,
            client_id=str(audience[0] if isinstance(audience, list) else audience),
            scopes=scopes,
            expires_at=int(claims["exp"]),
            resource=self.config.resource_url,
            subject=str(claims["sub"]),
            claims=claims,
        )

    def scopes(self, claims: dict[str, Any]) -> list[str]:
        granted = _entitlements(claims)
        if granted.intersection(self.config.write_claims):
            return [READ_SCOPE, WRITE_SCOPE]
        if granted.intersection(self.config.read_claims):
            return [READ_SCOPE]
        return []


async def _respond(send: Send, status: int, body: bytes, content_type: str) -> None:
    headers = [(b"content-type", content_type.encode()), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def forbidden(send: Send, error: str, description: str) -> None:
    body = json.dumps({"error": error, "error_description": description}).encode("utf-8")
    await _respond(send, 403, body, "application/json")


def header_value(scope: Scope, name: str) -> str:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


class ForwardedIdentity:
    # Requests forwarded by a trusted proxy carry a signed identity in one header and no bearer token.
    # A request without a valid one gets 403 and no WWW-Authenticate challenge: advertising an
    # authorization server here would point clients past the proxy that replaces it.
    def __init__(
        self,
        app: ASGIApp,
        verifier: ForwardedVerifier,
        header: str,
        health_path: str = "/up",
        open_prefixes: tuple[str, ...] = (),
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.header = header
        self.health_path = health_path
        self.open_prefixes = open_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == self.health_path:
            await _respond(send, 200, b"ok", "text/plain")
            return
        if any(path == prefix or path.startswith(prefix + "/") for prefix in self.open_prefixes):
            await self.app(scope, receive, send)
            return
        token = header_value(scope, self.header)
        if not token:
            await forbidden(send, "invalid_token", "missing forwarded identity")
            return
        try:
            access = self.verifier.verify(token)
        except IdentityError as exc:
            await forbidden(send, exc.error, exc.description)
            return
        user = AuthenticatedUser(access)
        authenticated = {**scope, "user": user, "auth": AuthCredentials(access.scopes)}
        context = auth_context_var.set(user)
        try:
            await self.app(authenticated, receive, send)
        finally:
            auth_context_var.reset(context)
