from __future__ import annotations

from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from notes_vault_mcp.backends import NotFound, VersionConflict
from notes_vault_mcp.backends.local import LocalBackend
from notes_vault_mcp.backends.s3 import S3Backend

BUCKET = "vault-test"
ENDPOINT = "https://s3.amazonaws.com"


def test_local_list_skips_obsidian_and_keeps_schema(vault_dir: Path):
    keys = {entry.key for entry in LocalBackend(vault_dir).list()}
    assert ".vault/schema.yml" in keys
    assert "Areas/greenhouse.md" in keys
    assert not any(key.startswith(".obsidian/") for key in keys)


def test_local_get_returns_text_and_version(vault_dir: Path):
    text, version = LocalBackend(vault_dir).get("Areas/greenhouse.md")
    assert text.startswith("---")
    assert ":" in version


def test_local_get_missing_raises_not_found(vault_dir: Path):
    with pytest.raises(NotFound):
        LocalBackend(vault_dir).get("Areas/nope.md")


def test_local_put_returns_a_new_version(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    _, before = backend.get("Areas/greenhouse.md")
    after = backend.put("Areas/greenhouse.md", "changed")
    assert after != before
    assert backend.get("Areas/greenhouse.md")[0] == "changed"


def test_local_put_with_a_stale_version_conflicts(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    _, version = backend.get("Areas/greenhouse.md")
    backend.put("Areas/greenhouse.md", "someone else wrote")
    with pytest.raises(VersionConflict):
        backend.put("Areas/greenhouse.md", "mine", expected_version=version)


def test_local_move_and_delete(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.move("Areas/greenhouse.md", "Archive/greenhouse.md")
    assert backend.get("Archive/greenhouse.md")[0].startswith("---")
    backend.delete("Archive/greenhouse.md")
    with pytest.raises(NotFound):
        backend.get("Archive/greenhouse.md")


@pytest.fixture
def s3_backend():
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
        backend = S3Backend(ENDPOINT, "key", "secret", BUCKET, prefix="vault")
        backend.put("Areas/greenhouse.md", "---\ntitle: greenhouse\n---\n\nbody\n")
        backend.put("Notes/skip.txt", "not a note")
        yield backend


def test_s3_list_only_returns_vault_keys(s3_backend: S3Backend):
    assert [entry.key for entry in s3_backend.list()] == ["Areas/greenhouse.md"]


def test_s3_list_strips_the_prefix_from_keys(s3_backend: S3Backend):
    raw = s3_backend.client.list_objects_v2(Bucket=BUCKET)["Contents"]
    assert {obj["Key"] for obj in raw} == {"vault/Areas/greenhouse.md", "vault/Notes/skip.txt"}


def test_s3_get_returns_the_etag_as_version(s3_backend: S3Backend):
    text, version = s3_backend.get("Areas/greenhouse.md")
    assert "greenhouse" in text
    assert '"' not in version and len(version) == 32


def test_s3_get_missing_raises_not_found(s3_backend: S3Backend):
    with pytest.raises(NotFound):
        s3_backend.get("Areas/nope.md")


def test_s3_put_with_a_stale_etag_conflicts(s3_backend: S3Backend):
    _, version = s3_backend.get("Areas/greenhouse.md")
    s3_backend.put("Areas/greenhouse.md", "someone else wrote")
    with pytest.raises(VersionConflict):
        s3_backend.put("Areas/greenhouse.md", "mine", expected_version=version)


def test_s3_put_with_the_current_etag_succeeds(s3_backend: S3Backend):
    _, version = s3_backend.get("Areas/greenhouse.md")
    s3_backend.put("Areas/greenhouse.md", "mine", expected_version=version)
    assert s3_backend.get("Areas/greenhouse.md")[0] == "mine"


def test_s3_move_and_delete(s3_backend: S3Backend):
    s3_backend.move("Areas/greenhouse.md", "Archive/greenhouse.md")
    assert s3_backend.get("Archive/greenhouse.md")[0].startswith("---")
    with pytest.raises(NotFound):
        s3_backend.get("Areas/greenhouse.md")
    s3_backend.delete("Archive/greenhouse.md")
    assert s3_backend.list() == []


def test_s3_list_all_returns_every_object_and_directory_markers(s3_backend: S3Backend):
    s3_backend.mkdir("Attachments/")
    s3_backend.put_bytes("Attachments/pic.png", b"\x89PNG", "image/png")
    keys = [entry.key for entry in s3_backend.list_all()]
    assert keys == ["Areas/greenhouse.md", "Attachments/", "Attachments/pic.png", "Notes/skip.txt"]
    assert [entry.key for entry in s3_backend.list_all("Attachments/")] == ["Attachments/", "Attachments/pic.png"]
    assert next(entry for entry in s3_backend.list_all() if entry.key == "Attachments/").is_dir


def test_s3_bytes_round_trip_with_a_content_type(s3_backend: S3Backend):
    version = s3_backend.put_bytes("Attachments/pic.png", bytes(range(256)))
    data, read_version = s3_backend.get_bytes("Attachments/pic.png")
    assert data == bytes(range(256))
    assert read_version == version
    head = s3_backend.client.head_object(Bucket=BUCKET, Key="vault/Attachments/pic.png")
    assert head["ContentType"] == "image/png"


def test_s3_get_bytes_missing_raises_not_found(s3_backend: S3Backend):
    with pytest.raises(NotFound):
        s3_backend.get_bytes("Attachments/nope.png")


def test_local_list_all_includes_directories_and_non_notes(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.put_bytes("Attachments/pic.png", b"png")
    keys = [entry.key for entry in backend.list_all()]
    assert "Attachments/" in keys and "Attachments/pic.png" in keys
    assert ".obsidian/" in keys
    assert [entry.key for entry in backend.list_all("Attachments/")] == ["Attachments/", "Attachments/pic.png"]


def test_local_delete_removes_a_whole_directory(vault_dir: Path):
    backend = LocalBackend(vault_dir)
    backend.put_bytes("Tmp/a/b.txt", b"x")
    backend.delete("Tmp/")
    assert not (vault_dir / "Tmp").exists()


def test_make_subject_backend_nests_the_subject_under_the_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from notes_vault_mcp.backends import make_subject_backend

    monkeypatch.setenv("VAULT_PATH", str(tmp_path))
    for name in ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    backend, identifier = make_subject_backend("1042", "users")
    assert isinstance(backend, LocalBackend)
    assert backend.root == (tmp_path / "users" / "1042").resolve()
    other, other_identifier = make_subject_backend("1187", "users")
    assert identifier != other_identifier


def test_make_subject_backend_extends_the_s3_prefix(monkeypatch: pytest.MonkeyPatch):
    from notes_vault_mcp.backends import make_subject_backend

    monkeypatch.delenv("VAULT_PATH", raising=False)
    monkeypatch.setenv("S3_ENDPOINT", ENDPOINT)
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    monkeypatch.setenv("S3_PREFIX", "vault")
    with mock_aws():
        backend, _ = make_subject_backend("1042", "users")
    assert isinstance(backend, S3Backend)
    assert backend.prefix == "vault/users/1042/"
