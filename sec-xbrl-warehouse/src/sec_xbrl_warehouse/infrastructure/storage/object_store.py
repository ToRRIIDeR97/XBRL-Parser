from __future__ import annotations

import shutil
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import StoredObject
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes, sha256_file


class LocalObjectStore:
    """Filesystem implementation with the same immutable-key contract as S3."""

    def __init__(self, root: Path, bucket: str) -> None:
        self.root = root
        self.bucket = bucket

    @property
    def bucket_root(self) -> Path:
        return self.root / self.bucket

    def ensure_bucket(self) -> None:
        self.bucket_root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        normalized = key.lstrip("/")
        path = (self.bucket_root / normalized).resolve()
        if self.bucket_root.resolve() not in path.parents and path != self.bucket_root.resolve():
            raise ValueError("Object key escapes the bucket root")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def put_file(
        self,
        source: Path,
        key: str,
        *,
        expected_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        del metadata
        self.ensure_bucket()
        sha256 = sha256_file(source)
        if expected_sha256 and sha256 != expected_sha256:
            raise ArtifactIntegrityError(
                f"Object checksum mismatch for {source}: expected {expected_sha256}, got {sha256}"
            )
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing_sha = sha256_file(destination)
            if existing_sha != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            shutil.copy2(source, destination)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=destination.stat().st_size,
            checksum_sha256=sha256,
            local_path=destination,
        )

    def get_file(self, key: str, destination: Path) -> StoredObject:
        source = self._path(key)
        if not source.is_file():
            raise FileNotFoundError(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=source.stat().st_size,
            checksum_sha256=sha256_file(source),
            local_path=destination,
        )

    def put_bytes(
        self,
        data: bytes,
        key: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        del metadata
        self.ensure_bucket()
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        sha256 = sha256_bytes(data)
        if destination.exists():
            if sha256_file(destination) != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            destination.write_bytes(data)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=len(data),
            checksum_sha256=sha256,
            local_path=destination,
        )


class S3ObjectStore:
    """S3-compatible immutable object store adapter."""

    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.object_store_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.object_store_endpoint,
            aws_access_key_id=settings.object_store_access_key,
            aws_secret_access_key=settings.object_store_secret_key,
            region_name=settings.object_store_region,
            use_ssl=settings.object_store_secure,
        )

    def ensure_bucket(self) -> None:
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self.client.create_bucket(Bucket=self.bucket)

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise

    def put_file(
        self,
        source: Path,
        key: str,
        *,
        expected_sha256: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self.ensure_bucket()
        sha256 = sha256_file(source)
        if expected_sha256 and sha256 != expected_sha256:
            raise ArtifactIntegrityError(
                f"Object checksum mismatch for {source}: expected {expected_sha256}, got {sha256}"
            )

        if self.exists(key):
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            existing_sha = head.get("Metadata", {}).get("sha256")
            if existing_sha and existing_sha != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            object_metadata = {"sha256": sha256, **(metadata or {})}
            self.client.upload_file(
                str(source),
                self.bucket,
                key,
                ExtraArgs={"Metadata": object_metadata},
            )
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=int(head["ContentLength"]),
            checksum_sha256=sha256,
            etag=str(head.get("ETag", "")).strip('"') or None,
        )

    def get_file(self, key: str, destination: Path) -> StoredObject:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(destination))
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        sha256 = head.get("Metadata", {}).get("sha256") or sha256_file(destination)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=destination.stat().st_size,
            checksum_sha256=sha256,
            etag=str(head.get("ETag", "")).strip('"') or None,
            local_path=destination,
        )

    def put_bytes(
        self,
        data: bytes,
        key: str,
        *,
        metadata: dict[str, str] | None = None,
    ) -> StoredObject:
        self.ensure_bucket()
        sha256 = sha256_bytes(data)
        if self.exists(key):
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            existing_sha = head.get("Metadata", {}).get("sha256")
            if existing_sha and existing_sha != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                Metadata={"sha256": sha256, **(metadata or {})},
            )
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=int(head["ContentLength"]),
            checksum_sha256=sha256,
            etag=str(head.get("ETag", "")).strip('"') or None,
        )


def create_object_store(settings: Settings) -> LocalObjectStore | S3ObjectStore:
    if settings.object_store_backend == "local":
        return LocalObjectStore(settings.data_dir / "objects", settings.object_store_bucket)
    return S3ObjectStore(settings)
