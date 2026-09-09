from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from mcp.server.auth.routes import create_protected_resource_routes
from pydantic import AnyHttpUrl, ConfigDict, TypeAdapter
from starlette.applications import Starlette
from starlette.routing import BaseRoute, Mount, Route
from starlette.types import Receive, Scope, Send

from notes_vault_mcp.auth import AuthConfig

URL = TypeAdapter(AnyHttpUrl, config=ConfigDict(url_preserve_empty_path=True))
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"


class Rewritten:
    def __init__(self, app: Starlette, path: str) -> None:
        self.app = app
        self.path = path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        rewritten = {**scope, "path": self.path, "raw_path": self.path.encode(), "root_path": ""}
        await self.app(rewritten, receive, send)


def mount_under_prefix(app: Starlette, auth: AuthConfig, server: Any) -> Starlette:
    routes: list[BaseRoute] = list(
        create_protected_resource_routes(
            resource_url=URL.validate_python(auth.resource_url),
            authorization_servers=[URL.validate_python(auth.issuer)],
            scopes_supported=list(auth.advertised_scopes),
        )
    )
    if auth.path_prefix and getattr(server, "_auth_server_provider", None) is not None:
        routes.append(_authorization_server_route(app, auth.path_prefix))
    routes.append(Mount(auth.path_prefix, app=app))
    return Starlette(routes=routes, lifespan=_forwarded_lifespan(app))


def _authorization_server_route(app: Starlette, prefix: str) -> Route:
    return Route(
        AUTHORIZATION_SERVER_PATH + prefix,
        endpoint=Rewritten(app, AUTHORIZATION_SERVER_PATH),
        methods=["GET", "OPTIONS"],
    )


def _forwarded_lifespan(app: Starlette) -> Callable[[Starlette], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with app.router.lifespan_context(app):
            yield

    return lifespan
