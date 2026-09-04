from __future__ import annotations

import shutil
import tempfile
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
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            snapshot = Path(handle.name)
        try:
            shutil.copy2(source, snapshot)
            sha256 = sha256_file(snapshot)
            if expected_sha256 and sha256 != expected_sha256:
                raise ArtifactIntegrityError(
                    f"Object checksum mismatch for {source}: expected {expected_sha256}, got {sha256}"
                )
            self._install(snapshot, destination, sha256, key)
        finally:
            snapshot.unlink(missing_ok=True)
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
        if source.resolve() != destination.resolve():
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
            try:
                shutil.copy2(source, temporary)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
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
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            handle.write(data)
            temporary = Path(handle.name)
        try:
            self._install(temporary, destination, sha256, key)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=len(data),
            checksum_sha256=sha256,
            local_path=destination,
        )

    @staticmethod
    def _install(source: Path, destination: Path, checksum: str, key: str) -> None:
        try:
            destination.hardlink_to(source)
        except FileExistsError:
            if sha256_file(destination) != checksum:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                ) from None


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
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            code = exc.response.get("Error", {}).get("Code")
            if status != 404 and code not in {"404", "NoSuchBucket", "NotFound"}:
                raise
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
            if existing_sha != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            object_metadata = {"sha256": sha256, **(metadata or {})}
            self._put_file_once(source, key, object_metadata, sha256)
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
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            self.client.download_file(self.bucket, key, str(temporary))
            actual = sha256_file(temporary)
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            expected = head.get("Metadata", {}).get("sha256")
            if expected and expected != actual:
                raise ArtifactIntegrityError(f"Stored object checksum mismatch: {key}")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=destination.stat().st_size,
            checksum_sha256=actual,
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
            if existing_sha != sha256:
                raise ArtifactIntegrityError(
                    f"Immutable object key already contains different bytes: {key}"
                )
        else:
            try:
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    Metadata={"sha256": sha256, **(metadata or {})},
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
                if status not in {409, 412}:
                    raise
                head = self.client.head_object(Bucket=self.bucket, Key=key)
                if head.get("Metadata", {}).get("sha256") != sha256:
                    raise
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        return StoredObject(
            bucket=self.bucket,
            key=key,
            size_bytes=int(head["ContentLength"]),
            checksum_sha256=sha256,
            etag=str(head.get("ETag", "")).strip('"') or None,
        )

    def _put_file_once(
        self, source: Path, key: str, metadata: dict[str, str], checksum: str
    ) -> None:
        """Atomically create an immutable key, including files over PutObject's limit."""
        upload_id: str | None = None
        try:
            if source.stat().st_size <= 5 * 1024**3:
                with source.open("rb") as handle:
                    self.client.put_object(
                        Bucket=self.bucket,
                        Key=key,
                        Body=handle,
                        ContentLength=source.stat().st_size,
                        Metadata=metadata,
                        IfNoneMatch="*",
                    )
                return
            started = self.client.create_multipart_upload(
                Bucket=self.bucket, Key=key, Metadata=metadata
            )
            upload_id = started["UploadId"]
            parts = []
            with source.open("rb") as handle:
                part_number = 1
                while data := handle.read(64 * 1024**2):
                    part = self.client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=data,
                    )
                    parts.append({"PartNumber": part_number, "ETag": part["ETag"]})
                    part_number += 1
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
                IfNoneMatch="*",
            )
            upload_id = None
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status not in {409, 412}:
                raise
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            if head.get("Metadata", {}).get("sha256") != checksum:
                raise ArtifactIntegrityError(
                    f"Concurrent writer used immutable key with different bytes: {key}"
                ) from exc
        finally:
            if upload_id:
                self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)


def create_object_store(settings: Settings) -> LocalObjectStore | S3ObjectStore:
    if settings.object_store_backend == "local":
        return LocalObjectStore(settings.data_dir / "objects", settings.object_store_bucket)
    return S3ObjectStore(settings)
