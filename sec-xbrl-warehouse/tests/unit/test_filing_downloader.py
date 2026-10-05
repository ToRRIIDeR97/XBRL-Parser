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
    # Without FilingSummary InputFiles, only the primary `.htm` is an inline input.
    assert "report.html" not in selected


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
    # Raw artifacts are never deleted by the application (no delete API).
    assert not hasattr(downloader, "delete_source_files")


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


def test_materialize_bounds_object_restore_and_preserves_destination(tmp_path: Path) -> None:
    settings = Settings(
        env="test",
        temp_dir=tmp_path / "tmp",
        filing_max_bytes=5,
    )
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, None, store)  # type: ignore[arg-type]
    filing = _filing()
    data = b"too large"
    checksum = sha256(data).hexdigest()
    name = filing.primary_document
    assert name is not None
    store.put_bytes(data, filing_object_key(filing, name, checksum))
    manifest = FilingPackageManifest(
        filing=filing,
        members=(
            PackageMember(
                name=name,
                source_url=f"{filing.archive_directory_url}/{name}",
                checksum_sha256=checksum,
                is_primary_document=True,
                is_xbrl_entrypoint=True,
            ),
        ),
        acquired_at=date.today(),  # type: ignore[arg-type]
    )
    destination = tmp_path / "out"
    destination.mkdir()
    target = destination / name
    target.write_bytes(b"existing")

    with pytest.raises(ArtifactIntegrityError, match="byte limit"):
        downloader.materialize_package(manifest, destination)

    assert target.read_bytes() == b"existing"


def test_cached_manifest_rejects_filing_and_member_shape_mismatch(tmp_path: Path) -> None:
    settings = Settings(env="test", temp_dir=tmp_path / "tmp")
    settings.temp_dir.mkdir(parents=True)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, None, store)  # type: ignore[arg-type]
    filing = _filing()
    name = filing.primary_document
    assert name is not None
    member = PackageMember(
        name=name,
        source_url=f"{filing.archive_directory_url}/{name}",
        checksum_sha256="0" * 64,
    )

    metadata_key = "manifest-metadata.json"
    metadata_manifest = FilingPackageManifest(
        filing=filing.model_copy(update={"form": "10-Q"}),
        members=(member,),
        acquired_at=datetime.now(UTC),
    )
    store.put_bytes(metadata_manifest.model_dump_json().encode(), metadata_key)
    with pytest.raises(ArtifactIntegrityError, match="metadata mismatch"):
        downloader._cached_manifest(metadata_key, filing, (name,))

    shape_key = "manifest-shape.json"
    shape_manifest = FilingPackageManifest(
        filing=filing,
        members=(member.model_copy(update={"name": "unexpected.xml"}),),
        acquired_at=datetime.now(UTC),
    )
    store.put_bytes(shape_manifest.model_dump_json().encode(), shape_key)
    with pytest.raises(ArtifactIntegrityError, match="do not match index"):
        downloader._cached_manifest(shape_key, filing, (name,))


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

    corrected = _filing().model_copy(update={"instance_document": "example-20251231.xsd"})
    third = await downloader.download_package(corrected)
    assert third.filing == corrected
    assert http.downloads == 4


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


@pytest.mark.parametrize("name", ["/tmp/outside.xml", "../outside.xml", "missing.xml"])
def test_entrypoint_must_be_contained_manifest_member(name):
    from sec_xbrl_warehouse.ingestion.filing_downloader import _choose_entrypoint

    manifest = FilingPackageManifest(
        filing=_filing().model_copy(update={"instance_document": name}),
        members=(),
        acquired_at=datetime.now(UTC),
    )
    with pytest.raises(ArtifactIntegrityError):
        _choose_entrypoint(manifest)


class _NamedHttp:
    """Serves fixed bytes by file name and counts requests."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    async def get_json(self, url: str):
        return {"directory": {"item": [{"name": name} for name in self.files]}}

    async def download(self, url: str, destination: Path, **kwargs):
        name = url.rsplit("/", 1)[-1]
        self.requested.append(name)
        data = self.files[name]
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, data)
        return destination, sha256(data).hexdigest(), len(data), {}


async def test_cached_manifest_reuses_stored_filing_summary(tmp_path: Path) -> None:
    summary = (
        b"<FilingSummary><InputFiles><File>example-20251231.htm</File>"
        b"<File>example-20251231_part2.htm</File></InputFiles></FilingSummary>"
    )
    http = _NamedHttp(
        {
            "example-20251231.htm": b"a",
            "example-20251231_part2.htm": b"b",
            "example-20251231.xsd": b"c",
            "FilingSummary.xml": summary,
        }
    )
    settings = Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, http, store)  # type: ignore[arg-type]
    first = await downloader.download_package(_filing())
    assert len(http.requested) == 4
    second = await downloader.download_package(_filing())
    assert len(http.requested) == 4, "a cache hit must not re-fetch FilingSummary.xml"
    assert second.members == first.members


async def test_xbrl_zip_fallback_manifest_is_reused_and_records_archive(tmp_path: Path) -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example-20251231.xml", b"<xbrl/>")
        archive.writestr("example-20251231_lab.xml", b"<linkbase/>")
        archive.writestr("example-20251231.xsd", b"<schema/>")
    zip_name = "0000000001-26-000001-xbrl.zip"
    http = _NamedHttp({"0000000001-26-000001.txt": b"x", zip_name: buffer.getvalue()})
    settings = Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, http, store)  # type: ignore[arg-type]
    first = await downloader.download_package(_filing())
    expanded = [member for member in first.members if member.source_archive]
    assert {member.source_archive for member in expanded} == {zip_name}
    assert [member.name for member in expanded if member.is_xbrl_entrypoint] == [
        "example-20251231.xml"
    ]
    assert all(member.source_url.endswith(zip_name) for member in expanded)
    second = await downloader.download_package(_filing())
    assert http.requested == [zip_name]
    assert second.members == first.members


async def test_concurrent_downloads_share_filing_byte_budget(tmp_path: Path) -> None:
    http = _NamedHttp({"example-20251231.htm": b"12345", "example-20251231.xsd": b"12345"})
    settings = Settings(
        env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data", filing_max_bytes=8
    )
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, http, store)  # type: ignore[arg-type]
    with pytest.raises(ArtifactIntegrityError, match="byte limit"):
        await downloader.download_package(_filing())


def test_malformed_filing_summary_is_rejected() -> None:
    payload = {"directory": {"item": [{"name": "example-20251231.htm"}]}}
    with pytest.raises(ArtifactIntegrityError, match="FilingSummary"):
        select_package_files(payload, _filing(), filing_summary=b"<FilingSummary>")


def test_inline_detection_reads_namespace_declarations(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import _declares_inline_xbrl

    inline = tmp_path / "inline.htm"
    inline.write_text(
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body/></html>',
        encoding="utf-8",
    )
    plain = tmp_path / "plain.htm"
    plain.write_text('<html xmlns="http://www.w3.org/1999/xhtml"><body/></html>')
    malformed = tmp_path / "malformed.htm"
    malformed.write_text("<html><body><p>exhibit</DOCUMENT>")
    entity = tmp_path / "entity.htm"
    entity.write_text('<!DOCTYPE html [<!ENTITY x SYSTEM "file:///etc/passwd">]><html>&x;</html>')
    assert _declares_inline_xbrl(inline)
    assert not _declares_inline_xbrl(plain)
    assert not _declares_inline_xbrl(malformed)
    assert not _declares_inline_xbrl(entity)


async def test_xbrl_zip_without_instance_is_rejected_and_archive_retained(
    tmp_path: Path,
) -> None:
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example-20251231.xsd", b"<schema/>")
        archive.writestr("example-20251231_lab.xml", b"<linkbase/>")
    zip_name = "0000000001-26-000001-xbrl.zip"
    data = buffer.getvalue()
    http = _NamedHttp({"0000000001-26-000001.txt": b"x", zip_name: data})
    settings = Settings(env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data")
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(settings, http, store)  # type: ignore[arg-type]
    with pytest.raises(ArtifactIntegrityError, match="no XBRL instance"):
        await downloader.download_package(_filing())
    assert store.exists(filing_object_key(_filing(), zip_name, sha256(data).hexdigest()))
    assert not store.exists(
        filing_object_key(_filing(), "example-20251231.xsd", sha256(b"<schema/>").hexdigest())
    )
