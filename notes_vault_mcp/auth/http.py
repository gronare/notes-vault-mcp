from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlsplit

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from notes_vault_mcp.auth import READ_SCOPE, SCOPES, AuthConfig
from notes_vault_mcp.vault import Vault


def auth_settings(auth: AuthConfig) -> AuthSettings:
    builtin = auth.mode == "builtin"
    return AuthSettings(
        issuer_url=auth.issuer,
        resource_server_url=auth.resource_url,
        required_scopes=[READ_SCOPE] if builtin else None,
        client_registration_options=ClientRegistrationOptions(
            enabled=builtin, valid_scopes=list(SCOPES), default_scopes=[READ_SCOPE]
        ),
        revocation_options=RevocationOptions(enabled=builtin),
    )


def server_auth_kwargs(auth: AuthConfig | None) -> dict[str, Any]:
    if auth is None or auth.mode == "bearer":
        return {}
    if auth.mode == "oidc":
        from notes_vault_mcp.auth.oidc import OidcVerifier

        return {"token_verifier": OidcVerifier(auth), "auth": auth_settings(auth)}
    from notes_vault_mcp.auth.builtin import BuiltinProvider

    return {"auth_server_provider": BuiltinProvider(auth), "auth": auth_settings(auth)}


class BearerToken:
    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or self._authorized(scope):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"unauthorized"})

    def _authorized(self, scope: dict) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                return secrets.compare_digest(value.decode("latin-1"), f"Bearer {self.token}")
        return False


LOCAL_HOSTS = ["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"]
LOCAL_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def transport_security(auth: AuthConfig) -> TransportSecuritySettings | None:
    if not auth.public_url:
        return None
    parts = urlsplit(auth.public_url)
    public_host = parts.hostname or ""
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[public_host, f"{public_host}:*", *LOCAL_HOSTS],
        allowed_origins=[f"{parts.scheme}://{parts.netloc}", *LOCAL_ORIGINS],
    )


def build_http_app(vault: Vault, auth: AuthConfig, host: str = "127.0.0.1") -> Any:
    from notes_vault_mcp.server import build_server

    server: MCPServer = build_server(vault, auth)
    if auth.mode == "builtin":
        provider = server._auth_server_provider
        provider.register_routes(server)
    app = server.streamable_http_app(host=host, transport_security=transport_security(auth))
    if auth.mode == "bearer":
        return BearerToken(app, auth.bearer_token)
    from notes_vault_mcp.auth.prefix import mount_under_prefix

    return mount_under_prefix(app, auth, server)
