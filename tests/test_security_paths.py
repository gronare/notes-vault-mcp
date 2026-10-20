from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import boto3
import pytest
from forwarded_support import Proxy, config
from moto import mock_aws
from test_notes import VALID

from notes_vault_mcp import notes
from notes_vault_mcp.auth.http import build_http_app
from notes_vault_mcp.auth.personal import PersonalTokens
from notes_vault_mcp.backends import NotFound, VaultError, make_subject_backend
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.backends.s3 import S3Backend
from notes_vault_mcp.dav import _clean
from notes_vault_mcp.server import _listing
from notes_vault_mcp.vault import SubjectVaults, subject_key

OBSIDIAN_SECRET = ".obsidian/plugins/remotely-save/data.json"
PLUGIN_CODE = ".obsidian/plugins/evil/main.js"


# --- the local backend --------------------------------------------------------------------------------------


def test_read_refuses_a_key_that_climbs_out_of_the_vault(vault, tmp_path: Path):
    (tmp_path / "secret.md").write_text("outside", encoding="utf-8")
    with pytest.raises(NotFound):
        notes.read(vault, "../secret.md")


def test_read_refuses_an_absolute_key(vault, tmp_path: Path):
    outside = tmp_path / "secret.md"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(NotFound):
        notes.read(vault, str(outside))


def test_write_never_lands_outside_the_vault(vault, tmp_path: Path):
    with pytest.raises(NotFound):
        notes.write(vault, "../escaped.md", VALID)
    with pytest.raises(NotFound):
        notes.write(vault, "Areas/../../escaped.md", VALID)
    assert not (tmp_path / "escaped.md").exists()


def test_move_never_lands_outside_the_vault(vault, tmp_path: Path):
    with pytest.raises(NotFound):
        notes.move(vault, "Areas/orchard.md", "../orchard.md")
    assert not (tmp_path / "orchard.md").exists()
    assert vault.backend.get("Areas/orchard.md")[0].startswith("---")


def test_a_symlink_pointing_outside_the_vault_is_neither_readable_nor_indexed(vault, vault_dir: Path, tmp_path: Path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntitle: outside\n---\n\nsecret\n", encoding="utf-8")
    (vault_dir / "Areas" / "link.md").symlink_to(outside)
    with pytest.raises(NotFound):
        notes.read(vault, "Areas/link.md")
    vault.index.sync(force=True)
    assert vault.index.note("Areas/link.md") is None


def test_a_symlink_inside_the_vault_is_followed(vault, vault_dir: Path):
    (vault_dir / "Areas" / "alias.md").symlink_to(vault_dir / "Areas" / "orchard.md")
    assert "title: orchard" in notes.read(vault, "Areas/alias.md")


def test_local_backend_refuses_an_absolute_key_on_every_operation(tmp_path: Path):
    backend = LocalBackend(tmp_path / "vault")
    outside = tmp_path / "x.md"
    for call in (
        lambda: backend.put(str(outside), "x"),
        lambda: backend.put_bytes(str(outside), b"x"),
        lambda: backend.get(str(outside)),
        lambda: backend.delete(str(outside)),
        lambda: backend.mkdir(str(tmp_path / "made")),
        lambda: backend.move("a.md", str(outside)),
    ):
        with pytest.raises(NotFound):
            call()
    assert not outside.exists() and not (tmp_path / "made").exists()


@pytest.mark.xfail(
    strict=True,
    reason="bug: LocalBackend keeps the root unresolved, so a symlinked root refuses every key",
)
def test_local_backend_works_when_the_root_is_reached_through_a_symlink(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    backend = LocalBackend(link)
    backend.put("Areas/x.md", "x")
    assert backend.get("Areas/x.md")[0] == "x"


@pytest.mark.parametrize("key", ["", ".", "Areas"])
@pytest.mark.xfail(
    strict=True,
    reason="bug: delete_file on a folder key (or the root) removes the whole tree in a local vault",
)
def test_delete_refuses_a_directory_key(vault, vault_dir: Path, key: str):
    with pytest.raises((VaultError, ValueError)):
        notes.delete(vault, key)
    assert (vault_dir / "Areas" / "orchard.md").is_file()


@pytest.mark.xfail(strict=True, reason="bug: move_file moves a whole folder when given a directory key")
def test_move_refuses_a_directory_source(vault, vault_dir: Path):
    with pytest.raises((VaultError, ValueError)):
        notes.move(vault, "Areas", "Archive/areas")
    assert (vault_dir / "Areas" / "orchard.md").is_file()


# --- keys outside the vault's note set ------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="bug: write_file accepts any key, so a prompt-injected client can plant an Obsidian plugin",
)
def test_write_refuses_a_key_under_the_obsidian_config(vault, vault_dir: Path):
    with pytest.raises((VaultError, ValueError)):
        notes.write(vault, PLUGIN_CODE, "alert(1)")
    assert not (vault_dir / PLUGIN_CODE).exists()


@pytest.mark.xfail(
    strict=True,
    reason="bug: append_file accepts any key, so it can create or extend files outside the note set",
)
def test_append_refuses_a_key_under_the_obsidian_config(vault, vault_dir: Path):
    with pytest.raises((VaultError, ValueError)):
        notes.append(vault, PLUGIN_CODE, "alert(1)")
    assert not (vault_dir / PLUGIN_CODE).exists()


@pytest.mark.xfail(
    strict=True,
    reason="bug: move_file accepts any destination, so a note body can be moved into the Obsidian config",
)
def test_move_refuses_a_destination_under_the_obsidian_config(vault, vault_dir: Path):
    with pytest.raises((VaultError, ValueError)):
        notes.move(vault, "Areas/orchard.md", PLUGIN_CODE)
    assert not (vault_dir / PLUGIN_CODE).exists()


@pytest.mark.xfail(
    strict=True,
    reason="bug: read_file serves files the index hides, such as the Remotely Save config with its credentials",
)
def test_read_refuses_the_obsidian_config(vault, vault_dir: Path):
    target = vault_dir / OBSIDIAN_SECRET
    target.parent.mkdir(parents=True)
    target.write_text('{"password": "hunter2"}', encoding="utf-8")
    with pytest.raises(NotFound):
        notes.read(vault, OBSIDIAN_SECRET)


def test_list_files_never_shows_the_obsidian_config(vault, vault_dir: Path):
    target = vault_dir / OBSIDIAN_SECRET
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")
    assert ".obsidian" not in _listing(vault, "")
    assert _listing(vault, ".obsidian/") == "No files under '.obsidian/'"


@pytest.mark.xfail(
    strict=True,
    reason="bug: a key with a parent segment is validated against the first folder named, not the one it lands in",
)
def test_a_key_with_a_parent_segment_is_validated_against_the_folder_it_lands_in(vault, vault_dir: Path):
    without_area = "---\ntitle: Sneaky\ndate: 2026-08-01\nupdated: 2026-08-01\ntags: [x]\nstatus: active\n---\n\nBody\n"
    with pytest.raises(notes.ValidationError):
        notes.write(vault, "Areas/../Projects/sneaky.md", without_area)
    assert not (vault_dir / "Projects" / "sneaky.md").exists()


def test_a_missing_note_error_names_the_key_and_not_the_vault_root(vault, vault_dir: Path):
    with pytest.raises(NotFound) as caught:
        notes.read(vault, "Areas/nope.md")
    assert str(vault_dir) not in str(caught.value)


# --- s3 -------------------------------------------------------------------------------------------------------


@pytest.fixture
def prefixed_s3():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="tenants")
        yield S3Backend("https://s3.amazonaws.com", "key", "secret", "tenants", prefix="vault/alice")


def test_s3_keys_never_leave_the_configured_prefix(prefixed_s3: S3Backend):
    for key in ("../bob/Areas/x.md", "/abs.md", "Areas/../../escape.md"):
        prefixed_s3.put(key, "x")
    stored = {obj["Key"] for obj in prefixed_s3.client.list_objects_v2(Bucket="tenants")["Contents"]}
    assert all(key.startswith("vault/alice/") for key in stored), stored


def test_s3_listing_strips_only_its_own_prefix(prefixed_s3: S3Backend):
    prefixed_s3.client.put_object(Bucket="tenants", Key="vault/bob/Areas/b.md", Body=b"bob")
    prefixed_s3.put("Areas/a.md", "alice")
    assert [entry.key for entry in prefixed_s3.list()] == ["Areas/a.md"]


# --- subjects -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("subject", ["../carl", "a/b", "", ".", "..", "-x", "x" * 65, "a b", "a\\b"])
def test_a_subject_that_could_name_a_path_is_hashed(subject: str):
    key = subject_key(subject)
    assert key.isalnum() and len(key) == 16


def test_a_plain_subject_keeps_its_name():
    assert subject_key("user.name@example.com") == "user.name@example.com"


def test_one_subjects_backend_cannot_reach_another_subjects_notes(storage: Path):
    bob, _ = make_subject_backend("bob", "users")
    bob.put("Areas/secret.md", "bob's")
    alice, _ = make_subject_backend("alice", "users")
    with pytest.raises(NotFound):
        alice.get("../bob/Areas/secret.md")
    assert alice.list() == []


# --- webdav ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["/../x", "/a/../x", "/%2e%2e/x", "/a/./x", "/a//x", "/a\\b", "/..%2fx", "/a/%2e/x"],
)
def test_dav_refuses_every_shape_of_a_climbing_or_odd_path(raw: str):
    assert _clean(raw) is None


@pytest.mark.parametrize("raw", ["/", "/Areas/", "/Areas/x.md", "/.vault/schema.yml", "/Areas/%C3%B6.md"])
def test_dav_keeps_a_plain_path(raw: str):
    assert _clean(raw) is not None


def _basic(secret: str) -> dict[str, str]:
    return {"Authorization": "Basic " + base64.b64encode(f"u:{secret}".encode()).decode("ascii")}


@pytest.fixture
def dav(vaults: SubjectVaults, proxy: Proxy, tmp_path: Path) -> tuple[Any, str]:
    vaults.for_subject("1042")
    secret = PersonalTokens(tmp_path / "auth").issue("1042")
    return build_http_app(vaults, config(proxy, tmp_path)), secret


async def _call(app: Any, method: str, path: str, headers: dict[str, str], body: bytes = b"") -> tuple[int, bytes]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "server": ("localhost", 80),
        "client": ("127.0.0.1", 1234),
    }
    await app(scope, receive, send)
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


@pytest.mark.anyio
async def test_dav_move_refuses_a_destination_that_climbs_out(dav: tuple[Any, str], storage: Path):
    app, secret = dav
    async with app.router.lifespan_context(app):
        await _call(app, "PUT", "/dav/Areas/mine.md", _basic(secret), b"mine")
        status, _ = await _call(
            app, "MOVE", "/dav/Areas/mine.md", {**_basic(secret), "Destination": "/dav/../Areas/escaped.md"}
        )
    assert status == 400
    assert not (storage / "users" / "Areas" / "escaped.md").exists()
    assert (storage / "users" / "1042" / "Areas" / "mine.md").is_file()


@pytest.mark.anyio
async def test_dav_move_destination_stays_inside_the_subjects_vault(dav: tuple[Any, str], storage: Path):
    app, secret = dav
    async with app.router.lifespan_context(app):
        await _call(app, "PUT", "/dav/Areas/mine.md", _basic(secret), b"mine")
        status, _ = await _call(
            app,
            "MOVE",
            "/dav/Areas/mine.md",
            {**_basic(secret), "Destination": "http://evil.example/dav/Areas/moved.md"},
        )
    assert status in (201, 204)
    assert (storage / "users" / "1042" / "Areas" / "moved.md").read_bytes() == b"mine"


@pytest.mark.anyio
async def test_a_climbing_path_under_dav_never_reaches_the_mcp_endpoint_unauthenticated(dav: tuple[Any, str]):
    app, _ = dav
    async with app.router.lifespan_context(app):
        status, body = await _call(app, "POST", "/dav/../mcp", {"content-type": "application/json"}, b"{}")
    assert status == 401
    assert b"jsonrpc" not in body


@pytest.mark.anyio
async def test_dav_basic_auth_with_an_undecodable_credential_is_refused(dav: tuple[Any, str]):
    app, _ = dav
    async with app.router.lifespan_context(app):
        status, _ = await _call(app, "PROPFIND", "/dav/", {"Authorization": "Basic not*base64", "Depth": "0"})
        assert status == 401
        status, _ = await _call(app, "PROPFIND", "/dav/", {"Authorization": "Digest abc", "Depth": "0"})
        assert status == 401


@pytest.mark.anyio
async def test_dav_ignores_the_username_and_trusts_only_the_token(dav: tuple[Any, str], tmp_path: Path):
    app, secret = dav
    other = PersonalTokens(tmp_path / "auth").issue("1187")
    headers = {"Authorization": "Basic " + base64.b64encode(f"user1042@example.com:{other}".encode()).decode()}
    async with app.router.lifespan_context(app):
        await _call(app, "PUT", "/dav/Areas/mine.md", _basic(secret), b"mine")
        status, _ = await _call(app, "GET", "/dav/Areas/mine.md", headers)
    assert status == 404
