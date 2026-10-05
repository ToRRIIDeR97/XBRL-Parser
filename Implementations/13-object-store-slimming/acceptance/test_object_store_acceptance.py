"""Worker-visible acceptance tests for 13-object-store-slimming."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from support13 import Control, Extractor, Http, PackageStore, Warehouse, big_bundle, filing, settings

from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import write_json
from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader, filing_object_key
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor


# AC-1
def test_ac1_extraction_checkpoint_is_compressed_and_resumable(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    bundle = big_bundle()
    extractor = Extractor(bundle)
    control = Control()
    ingestor = IncrementalFilingIngestor(
        control, PackageStore(), extractor, ParquetBatchWriter(config, store), Warehouse(fail_once=True),
        settings=config, object_store=store,
    )
    job_id = uuid4()
    with pytest.raises(ConnectionError):
        asyncio.run(ingestor.ingest(filing(), job_id=job_id))
    result = asyncio.run(ingestor.ingest(filing(), job_id=job_id))

    assert result["status"] == "complete"
    assert extractor.calls == 1, "resume must read the saved extraction, not re-run Arelle"
    key = control.checkpoints[("extract", "default")]["object_key"]
    assert key.endswith(".json.zst")
    plain = tmp_path / "plain.json"
    write_json(bundle, plain, 1 << 30)
    stored = store.stat(key).size_bytes if hasattr(store, "stat") else (tmp_path / "objects" / "sec-xbrl" / key).stat().st_size
    assert stored <= plain.stat().st_size // 4


# AC-2
async def test_ac2_bronze_stores_one_package_archive_not_loose_members(tmp_path: Path) -> None:
    files = {"example-20251231.htm": b"<html>inline</html>" * 50, "example-20251231.xsd": b"<schema/>" * 50}
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings(tmp_path), Http(files), store)  # type: ignore[arg-type]

    manifest = await downloader.download_package(filing())

    assert manifest.package_object_key and store.exists(manifest.package_object_key)
    for member in manifest.members:
        assert member.checksum_sha256
        assert not store.exists(filing_object_key(manifest.filing, member.name, member.checksum_sha256))
    entrypoint = downloader.materialize_package(manifest, tmp_path / "materialized")
    assert entrypoint.read_bytes() == files["example-20251231.htm"]
