from __future__ import annotations

import mimetypes

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from notes_vault_mcp.backends import Entry, NotFound, VersionConflict, is_vault_key

MISSING_CODES = frozenset({"NoSuchKey", "404", "NotFound"})


def _etag(raw: str | None) -> str:
    return (raw or "").strip('"')


class S3Backend:
    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        prefix: str = "",
        region: str = "us-east-1",
    ) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint or None,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(signature_version="s3v4"),
        )

    def _object_key(self, key: str) -> str:
        return self.prefix + key

    def _vault_key(self, object_key: str) -> str:
        return object_key[len(self.prefix) :] if self.prefix else object_key

    def list(self) -> list[Entry]:
        entries: list[Entry] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix):
            for obj in page.get("Contents", []):
                key = self._vault_key(obj["Key"])
                if not is_vault_key(key):
                    continue
                entries.append(
                    Entry(
                        key=key,
                        version=_etag(obj.get("ETag")),
                        mtime=obj["LastModified"],
                        size=obj.get("Size", 0),
                    )
                )
        return sorted(entries, key=lambda entry: entry.key)

    def list_all(self, prefix: str = "") -> list[Entry]:
        entries: list[Entry] = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self.prefix + prefix):
            for obj in page.get("Contents", []):
                key = self._vault_key(obj["Key"])
                if not key:
                    continue
                entries.append(
                    Entry(key=key, version=_etag(obj.get("ETag")), mtime=obj["LastModified"], size=obj.get("Size", 0))
                )
        return sorted(entries, key=lambda entry: entry.key)

    def get(self, key: str) -> tuple[str, str]:
        data, version = self.get_bytes(key)
        return data.decode("utf-8"), version

    def get_bytes(self, key: str) -> tuple[bytes, str]:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                raise NotFound(key) from exc
            raise
        return response["Body"].read(), _etag(response.get("ETag"))

    def current_version(self, key: str) -> str | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=self._object_key(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                return None
            raise
        return _etag(response.get("ETag"))

    def put(self, key: str, text: str, expected_version: str | None = None) -> str:
        if expected_version is not None:
            current = self.current_version(key)
            if current != expected_version:
                raise VersionConflict(f"{key} changed since it was read (etag {current}, expected {expected_version})")
        return self.put_bytes(key, text.encode("utf-8"), "text/markdown; charset=utf-8")

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> str:
        response = self.client.put_object(
            Bucket=self.bucket,
            Key=self._object_key(key),
            Body=data,
            ContentType=content_type or mimetypes.guess_type(key)[0] or "application/octet-stream",
        )
        return _etag(response.get("ETag"))

    def mkdir(self, key: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=self._object_key(key.rstrip("/") + "/"), Body=b"")

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._object_key(key))

    def move(self, src: str, dst: str) -> None:
        try:
            self.client.copy_object(
                Bucket=self.bucket,
                Key=self._object_key(dst),
                CopySource={"Bucket": self.bucket, "Key": self._object_key(src)},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] in MISSING_CODES:
                raise NotFound(src) from exc
            raise
        self.delete(src)
