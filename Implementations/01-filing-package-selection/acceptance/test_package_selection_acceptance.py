"""Worker-visible acceptance tests for 01-filing-package-selection."""

from __future__ import annotations

import asyncio
import io
import shutil
import zipfile
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    select_package_files,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ACCESSION = "0000000001-26-000001"


def _filing(**updates: object) -> FilingRef:
    filing = FilingRef(
        accession=ACCESSION,
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/000000000126000001",
    )
    return filing.model_copy(update=updates)


def _index(*names: str) -> dict[str, object]:
    return {"directory": {"item": [{"name": name} for name in names]}}


class _Http:
    """Serves fixed bytes per file name and records download concurrency."""

    def __init__(self, files: dict[str, bytes], delay: float = 0.0) -> None:
        self.files = files
        self.delay = delay
        self.requested: list[str] = []
        self.in_flight = 0
        self.peak = 0

    async def get_json(self, url: str) -> dict[str, object]:
        return _index(*self.files)

    async def download(self, url: str, destination: Path, **kwargs: object):
        name = url.rsplit("/", 1)[-1]
        self.requested.append(name)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            data = self.files[name]
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            return destination, sha256(data).hexdigest(), len(data), {}
        finally:
            self.in_flight -= 1


def _settings(tmp_path: Path, **updates: object) -> Settings:
    return Settings(
        env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data", **updates
    )


# AC-1
def test_ac1_selection_keeps_xbrl_inputs_and_drops_rendered_and_exhibit_files() -> None:
    payload = _index(
        "example-20251231.htm",
        "example-20251231_htm.xml",
        "example-20251231.xsd",
        "example-20251231_cal.xml",
        "example-20251231_def.xml",
        "example-20251231_lab.xml",
        "example-20251231_pre.xml",
        "FilingSummary.xml",
        "MetaLinks.json",
        "R1.htm",
        "R2.htm",
        "R12.htm",
        "ex31-certification.htm",
        "0000000001-26-000001-index.html",
        "0000000001-26-000001-index-headers.html",
        "0000000001-26-000001.txt",
        "logo.jpg",
        "Financial_Report.xlsx",
    )
    selected = set(select_package_files(payload, _filing()))
    assert selected == {
        "example-20251231.htm",
        "example-20251231_htm.xml",
        "example-20251231.xsd",
        "example-20251231_cal.xml",
        "example-20251231_def.xml",
        "example-20251231_lab.xml",
        "example-20251231_pre.xml",
        "FilingSummary.xml",
        "MetaLinks.json",
    }


# AC-3
@pytest.mark.arelle
def test_ac3_inline_document_set_excludes_exhibits_and_rendered_pages(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    package = tmp_path / "package"
    shutil.copytree(FIXTURES / "inline-package", package)
    settings = Settings(
        env="test",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
    )
    bundle = ArelleExtractor(settings).extract(package / "example-20251231.htm", _filing())

    codes = {issue.code for issue in bundle.validation_issues}
    assert "arelle:nonIxdsDocument" not in codes
    assert "xmlSchema:syntax" not in codes
    assert "R2.htm" not in bundle.metadata.source_documents
    assert "ex31-certification.htm" not in bundle.metadata.source_documents
    values = {fact.concept_name: fact for fact in bundle.facts}
    assert values["Revenue"].value_decimal == 1_234_000_000
    assert values["CompanyName"].value_text == "Example Corp"


# AC-4
async def test_ac4_xbrl_zip_fallback_when_index_lacks_xbrl_inputs(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("example-20251231.xml", b"<xbrl/>")
        archive.writestr("example-20251231.xsd", b"<schema/>")
    files = {
        f"{ACCESSION}.txt": b"full submission",
        f"{ACCESSION}-xbrl.zip": buffer.getvalue(),
    }
    payload = _index(*files)
    assert select_package_files(payload, _filing()) == (f"{ACCESSION}-xbrl.zip",)

    http = _Http(files)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(_settings(tmp_path), http, store)  # type: ignore[arg-type]
    manifest = await downloader.download_package(_filing())

    assert f"{ACCESSION}.txt" not in http.requested
    names = {member.name for member in manifest.members}
    assert {"example-20251231.xml", "example-20251231.xsd"} <= names
    entrypoint = downloader.materialize_package(manifest, tmp_path / "materialized")
    assert entrypoint.name == "example-20251231.xml"


# AC-5
async def test_ac5_downloads_run_concurrently_within_budget_and_order_is_stable(
    tmp_path: Path,
) -> None:
    names = [
        "example-20251231.htm",
        "example-20251231.xsd",
        "example-20251231_cal.xml",
        "example-20251231_lab.xml",
        "example-20251231_pre.xml",
        "example-20251231_def.xml",
    ]
    http = _Http({name: name.encode() for name in names}, delay=0.05)
    settings = _settings(tmp_path, sec_max_concurrency=3)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    manifest = await FilingPackageDownloader(settings, http, store).download_package(  # type: ignore[arg-type]
        _filing()
    )

    assert 1 < http.peak <= 3
    assert [member.name for member in manifest.members] == sorted(names)
