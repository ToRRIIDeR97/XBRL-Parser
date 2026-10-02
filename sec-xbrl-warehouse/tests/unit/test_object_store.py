import io
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import (
    LocalObjectStore,
    S3ObjectStore,
    create_object_store,
)


def test_local_object_store_is_immutable(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "bucket")
    first = tmp_path / "first.bin"
    first.write_bytes(b"same")
    stored = store.put_file(first, "path/item.bin")
    assert store.exists(stored.key)
    first.write_bytes(b"mutated after upload")
    assert stored.local_path is not None
    assert stored.local_path.read_bytes() == b"same"

    second = tmp_path / "second.bin"
    second.write_bytes(b"different")
    with pytest.raises(ArtifactIntegrityError):
        store.put_file(second, "path/item.bin")


def test_local_object_store_contract_and_key_safety(tmp_path: Path) -> None:
    settings = Settings(
        env="test", data_dir=tmp_path, object_store_backend="local", object_store_bucket="bucket"
    )
    store = create_object_store(settings)
    assert isinstance(store, LocalObjectStore)
    stored = store.put_bytes(b"payload", "/nested/item.bin")
    copied = store.get_file(stored.key, tmp_path / "copy.bin")
    assert copied.checksum_sha256 == stored.checksum_sha256
    assert copied.local_path and copied.local_path.read_bytes() == b"payload"
    store.get_file(stored.key, stored.local_path)  # type: ignore[arg-type]
    with pytest.raises(ArtifactIntegrityError):
        store.put_bytes(b"different", stored.key)
    store.delete(stored.key)
    assert not store.exists(stored.key)
    with pytest.raises(FileNotFoundError):
        store.get_file("missing", tmp_path / "missing.bin")
    with pytest.raises(ValueError, match="escapes"):
        store.exists("../outside")
    source = tmp_path / "source.bin"
    source.write_bytes(b"source")
    with pytest.raises(ArtifactIntegrityError, match="checksum"):
        store.put_file(source, "bad.bin", expected_sha256="0" * 64)


def test_local_bounded_restore_preserves_existing_destination(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path, "bucket")
    store.put_bytes(b"too large", "source.bin")
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"existing")

    with pytest.raises(ArtifactIntegrityError, match="byte limit"):
        store.get_file("source.bin", destination, max_bytes=3)

    assert destination.read_bytes() == b"existing"


def _client_error(status: int, code: str, operation: str) -> ClientError:
    return ClientError(
        {"ResponseMetadata": {"HTTPStatusCode": status}, "Error": {"Code": code}},
        operation,
    )


class _FakeS3:
    def __init__(self) -> None:
        self.bucket_exists = False
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.head_object_calls = 0
        self.last_body: _FakeBody | None = None
        self.content_length_overrides: dict[str, int] = {}

    def head_bucket(self, **kwargs: object) -> dict[str, object]:
        if not self.bucket_exists:
            raise _client_error(404, "NoSuchBucket", "HeadBucket")
        return {}

    def create_bucket(self, **kwargs: object) -> None:
        self.bucket_exists = True

    def head_object(self, *, Key: str, **kwargs: object) -> dict[str, object]:
        self.head_object_calls += 1
        if Key not in self.objects:
            raise _client_error(404, "NotFound", "HeadObject")
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": metadata, "ETag": '"etag"'}

    def put_object(
        self,
        *,
        Key: str,
        Body: object,
        Metadata: dict[str, str],
        IfNoneMatch: str,
        **kwargs: object,
    ) -> None:
        if Key in self.objects and IfNoneMatch == "*":
            raise _client_error(412, "PreconditionFailed", "PutObject")
        data = Body.read() if hasattr(Body, "read") else Body
        assert isinstance(data, bytes)
        self.objects[Key] = data, Metadata

    def get_object(self, *, Key: str, **kwargs: object) -> dict[str, object]:
        data, metadata = self.objects[Key]
        self.last_body = _FakeBody(data)
        return {
            "Body": self.last_body,
            "ContentLength": self.content_length_overrides.get(Key, len(data)),
            "Metadata": metadata,
            "ETag": '"etag"',
        }

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        Path(destination).write_bytes(self.objects[key][0])

    def delete_object(self, *, Key: str, **kwargs: object) -> None:
        self.objects.pop(Key, None)


class _FakeBody:
    def __init__(self, data: bytes) -> None:
        self._stream = io.BytesIO(data)
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        self.closed = True
        self._stream.close()


def test_s3_object_store_immutable_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeS3()
    monkeypatch.setattr(
        "sec_xbrl_warehouse.infrastructure.storage.object_store.boto3.client",
        lambda *args, **kwargs: client,
    )
    store = S3ObjectStore(Settings(env="test"))
    store.ensure_bucket()
    store.ensure_bucket()
    assert store.exists("missing") is False

    first = store.put_bytes(b"one", "one.bin", metadata={"kind": "test"})
    assert first.etag == "etag"
    assert store.put_bytes(b"one", "one.bin").checksum_sha256 == first.checksum_sha256
    with pytest.raises(ArtifactIntegrityError):
        store.put_bytes(b"changed", "one.bin")

    source = tmp_path / "two.bin"
    source.write_bytes(b"two")
    second = store.put_file(source, "two.bin", expected_sha256=None)
    destination = tmp_path / "downloaded.bin"
    head_calls_before_restore = client.head_object_calls
    downloaded = store.get_file(second.key, destination)
    assert downloaded.checksum_sha256 == second.checksum_sha256
    assert destination.read_bytes() == b"two"
    assert client.head_object_calls == head_calls_before_restore
    assert client.last_body is not None and client.last_body.closed
    store.delete(first.key)
    assert not store.exists(first.key)
    with pytest.raises(ArtifactIntegrityError, match="expected"):
        store.put_file(source, "wrong.bin", expected_sha256="0" * 64)

    data, metadata = client.objects[second.key]
    client.objects[second.key] = data, {**metadata, "sha256": "0" * 64}
    with pytest.raises(ArtifactIntegrityError, match="Stored object"):
        store.get_file(second.key, tmp_path / "corrupt.bin")


def test_s3_bounded_stream_preserves_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeS3()
    monkeypatch.setattr(
        "sec_xbrl_warehouse.infrastructure.storage.object_store.boto3.client",
        lambda *args, **kwargs: client,
    )
    store = S3ObjectStore(Settings(env="test"))
    store.put_bytes(b"payload", "source.bin")
    client.content_length_overrides["source.bin"] = 1
    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"existing")

    with pytest.raises(ArtifactIntegrityError, match="byte limit"):
        store.get_file("source.bin", destination, max_bytes=5)

    assert destination.read_bytes() == b"existing"
    assert client.last_body is not None and client.last_body.closed
