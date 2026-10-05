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


async def test_minio_archive_only_package_listing_and_report(tmp_path: Path) -> None:
    import asyncio
    from datetime import date
    from hashlib import sha256

    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.infrastructure.storage.inventory import storage_report
    from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader

    files = {"example-20251231.htm": b"<html>inline</html>", "example-20251231.xsd": b"<schema/>"}

    class Http:
        async def get_json(self, url: str):
            return {"directory": {"item": [{"name": name} for name in files]}}

        async def download(self, url: str, destination: Path, **kwargs):
            data = files[url.rsplit("/", 1)[-1]]
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(destination.write_bytes, data)
            return destination, sha256(data).hexdigest(), len(data), {}

    bucket = f"xbrl-test-{uuid4().hex}"
    settings = Settings(
        env="test",
        object_store_bucket=bucket,
        temp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
    )
    assert settings.object_store_bucket.startswith("xbrl-test-")
    store = S3ObjectStore(settings)
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    try:
        store.ensure_bucket()
        downloader = FilingPackageDownloader(settings, Http(), store)  # type: ignore[arg-type]
        manifest = await downloader.download_package(filing)
        keys = [key for key, _ in store.list_objects()]
        assert manifest.package_object_key in keys
        assert [key for key in keys if key.startswith("bronze/")] == [manifest.package_object_key]
        assert [key for key, _ in store.list_objects("manifests/")] == [
            key for key in keys if key.startswith("manifests/")
        ]
        entrypoint = downloader.materialize_package(manifest, tmp_path / "out")
        assert entrypoint.read_bytes() == files["example-20251231.htm"]
        report = storage_report(store, settings)
        assert report["usage"]["bronze_packages"]["objects"] == 1
        assert report["reclaimable"]["loose_members_in_archives"]["objects"] == 0
        assert [key for key, _ in store.list_objects()] == keys
    finally:
        response = store.client.list_objects_v2(Bucket=bucket)
        for item in response.get("Contents", []):
            store.client.delete_object(Bucket=bucket, Key=item["Key"])
        store.client.delete_bucket(Bucket=bucket)
