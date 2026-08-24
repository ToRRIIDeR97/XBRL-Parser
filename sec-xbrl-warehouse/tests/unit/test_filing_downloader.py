from datetime import date
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

    materialized = downloader.materialize_package(archived, tmp_path / "out")
    assert materialized.name == "example-20251231.htm"
    assert materialized.is_file()


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
