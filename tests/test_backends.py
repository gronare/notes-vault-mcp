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
