"""Independent verification tests for 13-object-store-slimming."""

from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from support13v import Control, Extractor, Http, PackageStore, Warehouse, big_bundle, filing, settings

from sec_xbrl_warehouse.domain.models import FilingPackageManifest, PackageMember
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.infrastructure.storage.parquet import ParquetBatchWriter
from sec_xbrl_warehouse.infrastructure.xbrl.extraction_io import write_json
from sec_xbrl_warehouse.ingestion.filing_downloader import FilingPackageDownloader, filing_object_key
from sec_xbrl_warehouse.ingestion.incremental import IncrementalFilingIngestor


# AC-3: manifests written before this change (loose members, no archive) still materialize.
def test_ac3_legacy_loose_member_manifest_materializes(tmp_path: Path) -> None:
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    data = b"<html>legacy</html>"
    checksum = sha256(data).hexdigest()
    store.put_bytes(data, filing_object_key(filing(), "example-20251231.htm", checksum))
    manifest = FilingPackageManifest(
        filing=filing(),
        members=(PackageMember(
            name="example-20251231.htm", source_url=f"{filing().archive_directory_url}/example-20251231.htm",
            size_bytes=len(data), checksum_sha256=checksum, is_primary_document=True, is_xbrl_entrypoint=True,
        ),),
        acquired_at=datetime.now(UTC),
    )
    downloader = FilingPackageDownloader(settings(tmp_path), Http({}), store)  # type: ignore[arg-type]
    assert downloader.materialize_package(manifest, tmp_path / "out").read_bytes() == data


# AC-3: a legacy uncompressed extraction checkpoint is still read on resume.
def test_ac3_legacy_json_checkpoint_resumes(tmp_path: Path) -> None:
    config = settings(tmp_path)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    bundle = big_bundle(20)
    path = tmp_path / "legacy.json"
    write_json(bundle, path, 1 << 30)
    checksum = sha256(path.read_bytes()).hexdigest()
    key = f"silver/xbrl/extractions/accession={bundle.filing.accession}/revision=1/sha256={checksum}.json"
    store.put_file(path, key, expected_sha256=checksum)
    control = Control()
    job_id = uuid4()
    control.checkpoints[("package", "default")] = asyncio.run(PackageStore().download_package(filing())).model_dump(mode="json")
    control.checkpoints[("extract", "default")] = {"object_key": key, "sha256": checksum}
    extractor = Extractor(bundle)
    ingestor = IncrementalFilingIngestor(
        control, PackageStore(), extractor, ParquetBatchWriter(config, store), Warehouse(),
        settings=config, object_store=store,
    )
    result = asyncio.run(ingestor.ingest(filing(), job_id=job_id))
    assert result["status"] == "complete"
    assert extractor.calls == 0


# AC-4: a tampered package archive is rejected when materialized.
async def test_ac4_tampered_archive_is_rejected(tmp_path: Path) -> None:
    files = {"example-20251231.htm": b"<html>inline</html>", "example-20251231.xsd": b"<schema/>"}
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings(tmp_path), Http(files), store)  # type: ignore[arg-type]
    manifest = await downloader.download_package(filing())

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example-20251231.htm", b"<html>tampered</html>")
        archive.writestr("example-20251231.xsd", b"<schema/>")
    forged_key = manifest.package_object_key.replace("sha256=", "sha256=forged-")
    store.put_bytes(buffer.getvalue(), forged_key)
    forged = manifest.model_copy(update={"package_object_key": forged_key})
    with pytest.raises(ArtifactIntegrityError):
        downloader.materialize_package(forged, tmp_path / "out")


# AC-2: archiving is deterministic, so a re-download of identical bytes reuses the same object key.
async def test_ac2_archive_key_is_deterministic(tmp_path: Path) -> None:
    files = {"example-20251231.htm": b"<html>inline</html>", "example-20251231.xsd": b"<schema/>"}
    first = await FilingPackageDownloader(settings(tmp_path / "a"), Http(files), LocalObjectStore(tmp_path / "o1", "b")).download_package(filing())  # type: ignore[arg-type]
    second = await FilingPackageDownloader(settings(tmp_path / "b"), Http(files), LocalObjectStore(tmp_path / "o2", "b")).download_package(filing())  # type: ignore[arg-type]
    assert first.package_object_key is not None
    assert first.package_object_key == second.package_object_key
    assert first.package_checksum_sha256 == second.package_checksum_sha256
