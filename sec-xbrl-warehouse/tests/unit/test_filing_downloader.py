import asyncio
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import (
    FilingPackageManifest,
    FilingRef,
    PackageMember,
)
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    filing_object_key,
    select_package_files,
)


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def test_select_package_files_includes_xbrl_and_primary() -> None:
    payload = {
        "directory": {
            "item": [
                {"name": "example-20251231.htm"},
                {"name": "example-20251231_htm.xml"},
                {"name": "example-20251231.xsd"},
                {"name": "report.html"},
                {"name": "logo.jpg"},
                {"name": "filingSummary.xml"},
            ]
        }
    }
    selected = select_package_files(payload, _filing())
    assert "example-20251231.htm" in selected
    assert "example-20251231_htm.xml" in selected
    assert "example-20251231.xsd" in selected
    assert "filingSummary.xml" in selected
    assert "logo.jpg" not in selected
    assert "report.html" in selected


def test_select_entrypoints_prefers_primary() -> None:
    payload = {
        "directory": {
            "item": [
                {"name": "example-20251231.htm"},
                {"name": "example-20251231_htm.xml"},
            ]
        }
    }
    entrypoints = FilingPackageDownloader.__new__(FilingPackageDownloader).select_xbrl_entrypoints(
        payload, _filing()
    )
    assert entrypoints[0] == "example-20251231.htm"


def test_archive_and_materialize_round_trip(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        temp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
    )
    settings.temp_dir.mkdir(parents=True, exist_ok=True)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    store.ensure_bucket()
    downloader = FilingPackageDownloader(settings, None, store)  # type: ignore[arg-type]

    filing = _filing()
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "example-20251231.htm").write_text(
        "<html><body>10-K</body></html>", encoding="utf-8"
    )
    (package_dir / "example-20251231_htm.xml").write_text("<xbrl/>", encoding="utf-8")

    members = []
    for name in ("example-20251231.htm", "example-20251231_htm.xml"):
        path = package_dir / name
        from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_file

        checksum = sha256_file(path)
        store.put_file(path, filing_object_key(filing, name, checksum))
        members.append(
            PackageMember(
                name=name,
                source_url=f"x/{name}",
                checksum_sha256=checksum,
                is_primary_document=name == filing.primary_document,
                is_xbrl_entrypoint=name == filing.primary_document,
            )
        )
    manifest = FilingPackageManifest(
        filing=filing,
        members=tuple(members),
        acquired_at=date.today(),  # type: ignore[arg-type]
    )

    archived = downloader.archive_package(manifest, package_dir)
    assert archived.package_object_key and archived.package_checksum_sha256
    replayed = downloader.archive_package(manifest, package_dir)
    assert replayed.package_checksum_sha256 == archived.package_checksum_sha256

    materialized = downloader.materialize_package(archived, tmp_path / "out")
    assert materialized.name == "example-20251231.htm"
    assert materialized.is_file()

    downloader.delete_source_files(archived)
    assert archived.package_object_key and not store.exists(archived.package_object_key)
    assert all(
        not store.exists(filing_object_key(filing, member.name, member.checksum_sha256))
        for member in members
        if member.checksum_sha256
    )


def test_materialize_refuses_path_traversal(tmp_path: Path) -> None:
    settings = Settings(env="test", temp_dir=tmp_path / "tmp")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, None, store)  # type: ignore[arg-type]
    filing = _filing()
    member = PackageMember(
        name="../escape.txt",
        source_url="x",
        checksum_sha256=None,
    )
    manifest = FilingPackageManifest(
        filing=filing,
        members=(member,),
        acquired_at=date.today(),  # type: ignore[arg-type]
    )
    with pytest.raises(ArtifactIntegrityError):
        downloader.materialize_package(manifest, tmp_path / "out")

    with pytest.raises(ArtifactIntegrityError):
        filing_object_key(filing, "..\\escape.txt", "0" * 64)


async def test_download_package_reuses_checksum_manifest(tmp_path: Path) -> None:
    class _Http:
        def __init__(self) -> None:
            self.downloads = 0

        async def get_json(self, url: str):
            return {
                "directory": {
                    "item": [
                        {"name": "example-20251231.htm"},
                        {"name": "example-20251231.xsd"},
                    ]
                }
            }

        async def download(self, url: str, destination: Path, **kwargs):
            self.downloads += 1
            data = url.rsplit("/", 1)[-1].encode()
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(destination.write_bytes, data)
            return destination, sha256(data).hexdigest(), len(data), {"content-type": "text/plain"}

    settings = Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    http = _Http()
    downloader = FilingPackageDownloader(settings, http, store)  # type: ignore[arg-type]
    first = await downloader.download_package(_filing())
    second = await downloader.download_package(_filing())
    assert http.downloads == 2
    assert first.members == second.members
    assert second.manifest_object_key
    assert second.acquired_at <= datetime.now(UTC)


async def test_storage_upload_keeps_event_loop_responsive(tmp_path):
    import threading

    pulse = threading.Event()
    threads = []

    class Store(LocalObjectStore):
        def put_file(self, *args, **kwargs):
            threads.append(threading.get_ident())
            assert pulse.wait(2), "Event loop was blocked by upload"
            return super().put_file(*args, **kwargs)

    class Http:
        async def get_json(self, url):
            return {"directory": {"item": []}}

        async def download(self, url, destination, **kwargs):
            destination.write_bytes(b"x")
            return destination, sha256(b"x").hexdigest(), 1, {}

    settings = Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")

    async def heartbeat():
        await asyncio.sleep(0.01)
        pulse.set()

    beat = asyncio.create_task(heartbeat())
    await FilingPackageDownloader(
        settings, Http(), Store(tmp_path / "objects", "test")
    ).download_package(_filing())
    await beat
    assert threads and threading.get_ident() not in threads
