import os
from pathlib import Path
from uuid import uuid4

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import S3ObjectStore

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


def test_minio_immutable_put_get_and_checksum(tmp_path: Path) -> None:
    bucket = f"xbrl-test-{uuid4().hex}"
    store = S3ObjectStore(Settings(env="test", object_store_bucket=bucket))
    try:
        store.ensure_bucket()
        first = store.put_bytes(b"immutable", "nested/item.bin")
        replay = store.put_bytes(b"immutable", "nested/item.bin")
        assert replay.checksum_sha256 == first.checksum_sha256
        with pytest.raises(ArtifactIntegrityError):
            store.put_bytes(b"changed", "nested/item.bin")

        downloaded = store.get_file("nested/item.bin", tmp_path / "item.bin")
        assert downloaded.checksum_sha256 == first.checksum_sha256
        assert downloaded.local_path and downloaded.local_path.read_bytes() == b"immutable"
    finally:
        response = store.client.list_objects_v2(Bucket=bucket)
        for item in response.get("Contents", []):
            store.client.delete_object(Bucket=bucket, Key=item["Key"])
        store.client.delete_bucket(Bucket=bucket)
