from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import html
import json
import secrets
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.mcpserver import MCPServer
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from notes_vault_mcp.auth import READ_SCOPE, WRITE_SCOPE, AuthConfig, auth_dir

CODE_TTL = 300
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
REQUEST_TTL = 900
MAX_FAILURES = 5
FAILURE_WINDOW = 60.0
LOCKOUT_SECONDS = 60.0
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    salt TEXT NOT NULL,
    hash TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    client_name TEXT NOT NULL,
    registered_at INTEGER NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    document TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    client_id TEXT NOT NULL,
    scopes TEXT NOT NULL,
    issued_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
"""

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>notes-vault-mcp</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; background: #f6f5f2; color: #1d1c1a;
  font: 15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
main {{ max-width: 26rem; margin: 4rem auto; padding: 2rem; background: #fff;
  border: 1px solid #e2e0da; border-radius: 10px; }}
h1 {{ margin: 0 0 .25rem; font-size: 1.25rem; }}
p {{ margin: 0 0 1.25rem; color: #56534d; }}
label {{ display: block; margin-bottom: .75rem; }}
input[type=password] {{ display: block; width: 100%; box-sizing: border-box; margin-top: .35rem;
  padding: .55rem .6rem; font-size: 1rem; border: 1px solid #cbc8c0; border-radius: 6px; }}
fieldset {{ margin: 0 0 1.25rem; padding: .75rem 1rem; border: 1px solid #e2e0da; border-radius: 6px; }}
legend {{ padding: 0 .35rem; color: #56534d; font-size: .85rem; }}
fieldset label {{ margin: 0; padding: .15rem 0; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
button {{ width: 100%; padding: .6rem; font-size: 1rem; color: #fff; background: #2f6f4f;
  border: 0; border-radius: 6px; cursor: pointer; }}
.error {{ margin-bottom: 1.25rem; padding: .6rem .75rem; color: #7a1f1f; background: #fbeceb;
  border: 1px solid #f0cfcc; border-radius: 6px; }}
</style>
</head>
<body>
<main>
<h1>{heading}</h1>
<p>{lead}</p>
{body}
</main>
</body>
</html>
"""

FORM = """{error}
<form method="post" action="{action}">
<input type="hidden" name="request" value="{request}">
<label>Owner password
<input type="password" name="password" autocomplete="current-password" autofocus required>
</label>
<fieldset>
<legend>Grant this client</legend>
<label><input type="checkbox" name="read" value="1" checked> read</label>
<label><input type="checkbox" name="write" value="1"> write</label>
</fieldset>
<button type="submit">Approve</button>
</form>
"""


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def password_hash(password: str, salt: bytes) -> str:
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return derived.hex()


def stamp(moment: int | float | None) -> str:
    if not moment:
        return "-"
    return datetime.fromtimestamp(float(moment), tz=UTC).strftime("%Y-%m-%d %H:%M")


class Store:
    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "auth.sqlite"
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def set_owner(self, password: str) -> None:
        salt = secrets.token_bytes(16)
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO owner(id, salt, hash, updated_at) VALUES(1, ?, ?, ?)",
                (salt.hex(), password_hash(password, salt), int(time.time())),
            )

    def owner_row(self) -> sqlite3.Row | None:
        with self.connect() as connection:
            row = connection.execute("SELECT salt, hash FROM owner WHERE id = 1").fetchone()
        return row

    def owner_matches(self, password: str) -> bool:
        row = self.owner_row()
        if row is None:
            return False
        return hmac.compare_digest(row["hash"], password_hash(password, bytes.fromhex(row["salt"])))

    def save_client(self, client: OAuthClientInformationFull) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO clients(client_id, client_name, registered_at, document) VALUES(?, ?, ?, ?)",
                (
                    client.client_id,
                    client.client_name or "unnamed client",
                    int(time.time()),
                    client.model_dump_json(exclude_none=True),
                ),
            )

    def load_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with self.connect() as connection:
            row = connection.execute("SELECT document FROM clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row["document"]) if row else None

    def client_rows(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT client_id, client_name, registered_at, document FROM clients ORDER BY registered_at"
            ).fetchall()

    def save_request(self, request_id: str, payload: dict[str, Any]) -> None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("DELETE FROM requests WHERE created_at < ?", (now - REQUEST_TTL,))
            connection.execute(
                "INSERT OR REPLACE INTO requests(id, created_at, document) VALUES(?, ?, ?)",
                (request_id, now, json.dumps(payload)),
            )

    def load_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT document FROM requests WHERE id = ? AND created_at >= ?",
                (request_id, int(time.time()) - REQUEST_TTL),
            ).fetchone()
        return json.loads(row["document"]) if row else None

    def drop_request(self, request_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM requests WHERE id = ?", (request_id,))

    def save_code(self, code: str, client_id: str, expires_at: float, payload: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM codes WHERE expires_at < ?", (time.time(),))
            connection.execute(
                "INSERT INTO codes(code_hash, client_id, expires_at, document) VALUES(?, ?, ?, ?)",
                (token_hash(code), client_id, expires_at, json.dumps(payload)),
            )

    def load_code(self, code: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT document FROM codes WHERE code_hash = ?", (token_hash(code),)).fetchone()
        return json.loads(row["document"]) if row else None

    def drop_code(self, code: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM codes WHERE code_hash = ?", (token_hash(code),))

    def save_token(self, token: str, kind: str, grant_id: str, client_id: str, scopes: list[str], ttl: int) -> int:
        issued_at = int(time.time())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO tokens(token_hash, kind, grant_id, client_id, scopes, issued_at, expires_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (token_hash(token), kind, grant_id, client_id, " ".join(scopes), issued_at, issued_at + ttl),
            )
        return issued_at + ttl

    def load_token(self, token: str, kind: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND revoked = 0",
                (token_hash(token), kind),
            ).fetchone()

    def revoke_grant(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE tokens SET revoked = 1 WHERE grant_id IN (SELECT grant_id FROM tokens WHERE token_hash = ?)",
                (token_hash(token),),
            )

    def revoke_one(self, token: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE tokens SET revoked = 1 WHERE token_hash = ?", (token_hash(token),))

    def revoke_client(self, client_id: str) -> int:
        with self.connect() as connection:
            revoked = connection.execute(
                "UPDATE tokens SET revoked = 1 WHERE client_id = ? AND revoked = 0", (client_id,)
            ).rowcount
        return revoked

    def newest_token(self, client_id: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM tokens WHERE client_id = ? AND kind = 'access' ORDER BY issued_at DESC, rowid DESC"
                " LIMIT 1",
                (client_id,),
            ).fetchone()


class BuiltinProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, config: AuthConfig) -> None:
        self.config = config
        self.store = Store(config.auth_dir or auth_dir())
        self.failures: dict[str, list[float]] = {}

    def register_routes(self, server: MCPServer) -> None:
        server.custom_route("/login", methods=["GET"])(self.login_page)
        server.custom_route("/login", methods=["POST"])(self.login_submit)

    async def login_page(self, request: Request) -> Response:
        request_id = request.query_params.get("request", "")
        pending = self.store.load_request(request_id) if request_id else None
        if pending is None:
            return self.expired_page()
        return HTMLResponse(self.render(request_id, pending, self.unconfigured()))

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        request_id = str(form.get("request") or "")
        pending = self.store.load_request(request_id) if request_id else None
        if pending is None:
            return self.expired_page()
        caller = request.client.host if request.client else "unknown"
        if self.locked_out(caller):
            return HTMLResponse(
                self.render(request_id, pending, "Too many failed attempts. Try again in a minute."), status_code=429
            )
        if not self.store.owner_matches(str(form.get("password") or "")):
            self.record_failure(caller)
            message = self.unconfigured() or "Wrong password."
            return HTMLResponse(self.render(request_id, pending, message), status_code=401)
        granted = self.granted_scopes(pending["scopes"], form.get("read") is not None, form.get("write") is not None)
        if not granted:
            return HTMLResponse(
                self.render(request_id, pending, "Tick at least one of read and write."), status_code=400
            )
        self.failures.pop(caller, None)
        self.store.drop_request(request_id)
        code = self.mint_code(pending, granted)
        return RedirectResponse(
            construct_redirect_uri(pending["redirect_uri"], code=code, state=pending["state"]),
            status_code=302,
            headers={"Cache-Control": "no-store"},
        )

    def unconfigured(self) -> str:
        if self.store.owner_row() is not None:
            return ""
        return "No owner password is set. Run `notes-vault-mcp owner set-password` on the server first."

    def granted_scopes(self, requested: list[str], read: bool, write: bool) -> list[str]:
        ticked = {READ_SCOPE} if read else set()
        if write:
            ticked.add(WRITE_SCOPE)
        return [scope for scope in requested if scope in ticked]

    def mint_code(self, pending: dict[str, Any], scopes: list[str]) -> str:
        code = new_secret()
        record = {
            "code": code,
            "client_id": pending["client_id"],
            "scopes": scopes,
            "expires_at": time.time() + CODE_TTL,
            "code_challenge": pending["code_challenge"],
            "redirect_uri": pending["redirect_uri"],
            "redirect_uri_provided_explicitly": pending["redirect_uri_provided_explicitly"],
            "resource": pending["resource"],
            "subject": "owner",
        }
        self.store.save_code(code, pending["client_id"], record["expires_at"], record)
        return code

    def locked_out(self, caller: str) -> bool:
        recent = self.recent_failures(caller)
        return len(recent) >= MAX_FAILURES and time.monotonic() - recent[-1] < LOCKOUT_SECONDS

    def recent_failures(self, caller: str) -> list[float]:
        cutoff = time.monotonic() - FAILURE_WINDOW
        recent = [moment for moment in self.failures.get(caller, []) if moment > cutoff]
        self.failures[caller] = recent
        return recent

    def record_failure(self, caller: str) -> None:
        self.failures.setdefault(caller, []).append(time.monotonic())

    def render(self, request_id: str, pending: dict[str, Any], error: str = "") -> str:
        client = self.store.load_client(pending["client_id"])
        name = (client.client_name if client else None) or pending["client_id"]
        body = FORM.format(
            error=f'<p class="error">{html.escape(error)}</p>' if error else "",
            action=html.escape(f"{self.config.path_prefix}/login"),
            request=html.escape(request_id),
        )
        return PAGE.format(
            heading="Vault access",
            lead=f"{html.escape(name)} is asking to reach your vault.",
            body=body,
        )

    def expired_page(self) -> Response:
        body = PAGE.format(
            heading="Nothing to approve",
            lead="This login link has expired or was already used. Start the connection again from the client.",
            body="",
        )
        return HTMLResponse(body, status_code=400)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.store.load_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_client(client_info)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        self.store.save_request(
            request_id,
            {
                "client_id": client.client_id,
                "state": params.state,
                "scopes": params.scopes or self.client_scopes(client),
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
        )
        return f"{self.config.public_url.rstrip('/')}/login?request={request_id}"

    def client_scopes(self, client: OAuthClientInformationFull) -> list[str]:
        return client.scope.split() if client.scope else [READ_SCOPE]

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = self.store.load_code(authorization_code)
        if record is None or record["client_id"] != client.client_id:
            return None
        return AuthorizationCode.model_validate(record)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self.store.load_code(authorization_code.code) is None:
            raise TokenError("invalid_grant", "authorization code does not exist")
        self.store.drop_code(authorization_code.code)
        if authorization_code.expires_at < time.time():
            raise TokenError("invalid_grant", "authorization code has expired")
        return self.issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            subject="owner",
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        if self.store.load_token(refresh_token.token, "refresh") is None:
            raise TokenError("invalid_grant", "refresh token does not exist")
        self.store.revoke_one(refresh_token.token)
        return self.issue(client.client_id, scopes or refresh_token.scopes)

    def issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        grant_id = secrets.token_urlsafe(16)
        access, refresh = new_secret(), new_secret()
        self.store.save_token(access, "access", grant_id, client_id, scopes, ACCESS_TTL)
        self.store.save_token(refresh, "refresh", grant_id, client_id, scopes, REFRESH_TTL)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ACCESS_TTL,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.load_token(token, "access")
        if row is None or row["expires_at"] <= time.time():
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=self.config.resource_url,
            subject="owner",
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.store.revoke_grant(token.token)


def command_owner(args: argparse.Namespace) -> int:
    store = Store(auth_dir())
    password = args.password or getpass.getpass("Owner password: ")
    if not password.strip():
        print("notes-vault-mcp: the owner password may not be empty", file=sys.stderr)
        return 2
    store.set_owner(password)
    print(f"notes-vault-mcp: owner password set in {store.path}")
    return 0


def command_tokens(args: argparse.Namespace) -> int:
    store = Store(auth_dir())
    if args.tokens_command == "revoke":
        print(f"notes-vault-mcp: revoked {store.revoke_client(args.client_id)} tokens for {args.client_id}")
        return 0
    rows = store.client_rows()
    if not rows:
        print("notes-vault-mcp: no client has been authorized yet")
        return 0
    for row in rows:
        print(token_row(store, row))
    return 0


def token_row(store: Store, row: sqlite3.Row) -> str:
    client = OAuthClientInformationFull.model_validate_json(row["document"])
    latest = store.newest_token(row["client_id"])
    scopes = latest["scopes"] if latest else (client.scope or "")
    issued = stamp(latest["issued_at"]) if latest else "-"
    expires = stamp(latest["expires_at"]) if latest else "-"
    state = "  revoked" if latest is not None and latest["revoked"] else ""
    return f"{row['client_id']}  {row['client_name']}  {scopes}  issued {issued}  expires {expires}{state}"
