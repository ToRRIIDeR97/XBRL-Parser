import asyncio
import io
import json
import zipfile
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sec_xbrl_warehouse import cli
from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.inventory import storage_report
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    filing_object_key,
)

ACCESSION = "0000000001-26-000001"


def _filing() -> FilingRef:
    return FilingRef(
        accession=ACCESSION,
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


class _Http:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def get_json(self, url: str):
        return {"directory": {"item": [{"name": name} for name in self.files]}}

    async def download(self, url: str, destination: Path, **kwargs):
        data = self.files[url.rsplit("/", 1)[-1]]
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, data)
        return destination, sha256(data).hexdigest(), len(data), {}


def _settings(tmp_path: Path) -> Settings:
    return Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")


def _keys(store: LocalObjectStore) -> list[str]:
    return [key for key, _ in store.list_objects()]


async def test_xbrl_zip_fallback_stores_only_the_package_archive(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example-20251231.xml", b"<xbrl/>")
        archive.writestr("example-20251231.xsd", b"<schema/>")
    source = buffer.getvalue()
    zip_name = f"{ACCESSION}-xbrl.zip"
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(_settings(tmp_path), _Http({zip_name: source}), store)  # type: ignore[arg-type]
    manifest = await downloader.download_package(_filing())

    assert not [key for key in _keys(store) if "/sha256=" in key and "/packages/" not in key]
    entrypoint = downloader.materialize_package(manifest, tmp_path / "out")
    assert entrypoint.name == "example-20251231.xml"
    assert (tmp_path / "out" / zip_name).read_bytes() == source
    assert (tmp_path / "out" / "example-20251231.xsd").read_bytes() == b"<schema/>"
    with zipfile.ZipFile(store.bucket_root / manifest.package_object_key) as package:
        embedded = json.loads(package.read("manifest.json"))
        assert {info.compress_type for info in package.infolist()} == {zipfile.ZIP_LZMA}
    assert set(embedded) == {"filing", "members"}


async def _archived(tmp_path: Path) -> tuple[FilingPackageDownloader, FilingPackageManifest]:
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    files = {"example-20251231.htm": b"<html/>", "example-20251231.xsd": b"<schema/>"}
    downloader = FilingPackageDownloader(_settings(tmp_path), _Http(files), store)  # type: ignore[arg-type]
    return downloader, await downloader.download_package(_filing())


def _forge(downloader, manifest, entries: dict[str, bytes]) -> FilingPackageManifest:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    data = buffer.getvalue()
    key = f"bronze/forged/{sha256(data).hexdigest()}.zip"
    downloader.object_store.put_bytes(data, key)
    return manifest.model_copy(
        update={"package_object_key": key, "package_checksum_sha256": sha256(data).hexdigest()}
    )


@pytest.mark.parametrize(
    "entries",
    [
        # An extra file not in the manifest.
        {"example-20251231.htm": b"<html/>", "example-20251231.xsd": b"<schema/>", "x.js": b"1"},
        # A manifest member missing from the archive.
        {"example-20251231.htm": b"<html/>"},
        # Matching archive checksum but member bytes differ from the manifest.
        {"example-20251231.htm": b"<html>evil</html>", "example-20251231.xsd": b"<schema/>"},
    ],
)
async def test_archive_contents_must_match_the_manifest(tmp_path: Path, entries) -> None:
    downloader, manifest = await _archived(tmp_path)
    forged = _forge(downloader, manifest, entries)
    with pytest.raises(ArtifactIntegrityError):
        downloader.materialize_package(forged, tmp_path / "out")


async def test_storage_report_lists_reclaimable_objects_without_deleting(tmp_path: Path) -> None:
    downloader, manifest = await _archived(tmp_path)
    store = downloader.object_store
    assert isinstance(store, LocalObjectStore)
    # Legacy loose copies of the archived members, and one orphan with no archive.
    for member in manifest.members:
        data = {"example-20251231.htm": b"<html/>", "example-20251231.xsd": b"<schema/>"}[
            member.name
        ]
        store.put_bytes(
            data, filing_object_key(manifest.filing, member.name, sha256(data).hexdigest())
        )
    orphan = filing_object_key(
        _filing().model_copy(update={"accession": "0000000001-26-000002"}), "a.htm", "0" * 64
    )
    store.put_bytes(b"orphan", orphan)
    base = f"silver/xbrl/extractions/accession={ACCESSION}"
    store.put_bytes(b"{}", f"{base}/revision=1/sha256={'1' * 64}.json")
    store.put_bytes(b"z", f"{base}/revision=1/sha256={'2' * 64}.json.zst")
    store.put_bytes(b"{}", f"{base}/revision=2/sha256={'3' * 64}.json")
    before = _keys(store)

    report = storage_report(store, downloader.settings)

    assert _keys(store) == before
    loose = report["reclaimable"]["loose_members_in_archives"]
    assert loose["objects"] == 2 and orphan not in loose["keys"]
    assert report["reclaimable"]["json_checkpoints_with_zst"]["keys"] == [
        f"{base}/revision=1/sha256={'1' * 64}.json"
    ]
    assert report["usage"]["bronze_packages"]["objects"] == 1
    assert report["usage"]["extraction_json_zst"]["objects"] == 1
    assert report["total"]["objects"] == len(before)

    keys_file = tmp_path / "keys.txt"
    settings = downloader.settings
    original = cli.get_settings
    cli.get_settings = lambda: settings  # type: ignore[assignment]
    original_store = cli.create_object_store
    cli.create_object_store = lambda _: store  # type: ignore[assignment]
    try:
        result = CliRunner().invoke(cli.app, ["storage", "report", "--keys-file", str(keys_file)])
    finally:
        cli.get_settings = original  # type: ignore[assignment]
        cli.create_object_store = original_store  # type: ignore[assignment]
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["dry_run"] is True
    assert len(keys_file.read_text().splitlines()) == 3
    assert _keys(store) == before
