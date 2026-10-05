"""Independent verification tests for 01-filing-package-selection."""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef, PackageMember
from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    FilingPackageDownloader,
    filing_object_key,
    select_package_files,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ACCESSION = "0000000002-19-000010"


def _filing(**updates: object) -> FilingRef:
    filing = FilingRef(
        accession=ACCESSION,
        cik=2,
        company_name="Verify Corp",
        form="10-Q",
        filed_date=date(2019, 1, 30),
        primary_document="r10q.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/2/000000000219000010",
    )
    return filing.model_copy(update=updates)


def _index(*names: str) -> dict[str, object]:
    return {"directory": {"item": [{"name": name} for name in names]}}


class _Http:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    async def get_json(self, url: str) -> dict[str, object]:
        return _index(*self.files)

    async def download(self, url: str, destination: Path, **kwargs: object):
        name = url.rsplit("/", 1)[-1]
        self.requested.append(name)
        data = self.files[name]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination, sha256(data).hexdigest(), len(data), {}


def _settings(tmp_path: Path, **updates: object) -> Settings:
    return Settings(
        env="test", temp_dir=tmp_path / "tmp", data_dir=tmp_path / "data", **updates
    )


# AC-1 boundary cases: legacy XML renderings, case, primary names that look like renders.
def test_ac1_legacy_renderings_case_and_primary_name_edges() -> None:
    payload = _index(
        "r10q.htm",
        "r10q.xml",
        "r10q.xsd",
        "R1.xml",
        "R2.xml",
        "R3.htm",
        "EX-32.HTM",
        "filingsummary.xml",
        "Show.js",
        "report.css",
    )
    selected = set(select_package_files(payload, _filing(instance_document="r10q.xml")))
    assert selected == {"r10q.htm", "r10q.xml", "r10q.xsd", "filingsummary.xml"}


# AC-2
def test_ac2_filing_summary_input_files_keep_multi_document_inline_sets() -> None:
    payload = _index(
        "example-20251231.htm",
        "example-20251231_part2.htm",
        "ex31-certification.htm",
        "example-20251231.xsd",
        "FilingSummary.xml",
        "R2.htm",
    )
    summary = (FIXTURES / "FilingSummary.xml").read_bytes()
    selected = set(
        select_package_files(
            payload,
            _filing(primary_document="example-20251231.htm"),
            filing_summary=summary,
        )
    )
    assert {"example-20251231.htm", "example-20251231_part2.htm"} <= selected
    assert "ex31-certification.htm" not in selected
    assert "R2.htm" not in selected


# AC-2 through the downloader: FilingSummary is fetched once and drives selection.
async def test_ac2_downloader_uses_filing_summary(tmp_path: Path) -> None:
    files = {
        "example-20251231.htm": b"a",
        "example-20251231_part2.htm": b"b",
        "ex31-certification.htm": b"c",
        "example-20251231.xsd": b"d",
        "FilingSummary.xml": (FIXTURES / "FilingSummary.xml").read_bytes(),
        "R2.htm": b"e",
    }
    http = _Http(files)
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    manifest = await FilingPackageDownloader(_settings(tmp_path), http, store).download_package(  # type: ignore[arg-type]
        _filing(primary_document="example-20251231.htm")
    )
    names = {member.name for member in manifest.members}
    assert "example-20251231_part2.htm" in names
    assert "ex31-certification.htm" not in http.requested
    assert "R2.htm" not in http.requested
    assert http.requested.count("FilingSummary.xml") == 1


# AC-3 boundary: a well-formed XHTML exhibit without the inline namespace.
@pytest.mark.arelle
def test_ac3_wellformed_non_inline_exhibit_is_not_in_document_set(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    package = tmp_path / "package"
    shutil.copytree(FIXTURES / "inline-package", package)
    (package / "ex31-certification.htm").unlink()
    (package / "R2.htm").unlink()
    (package / "ex99.htm").write_text(
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml">'
        "<body><p>Press release</p></body></html>",
        encoding="utf-8",
    )
    settings = Settings(
        env="test",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
    )
    bundle = ArelleExtractor(settings).extract(
        package / "example-20251231.htm",
        _filing(primary_document="example-20251231.htm"),
    )
    assert "ex99.htm" not in bundle.metadata.source_documents
    assert "arelle:nonIxdsDocument" not in {issue.code for issue in bundle.validation_issues}
    assert len(bundle.facts) == 2


# AC-6: a manifest cached under the pre-change policy must not block re-ingestion.
async def test_ac6_manifest_cached_by_previous_policy_does_not_block(tmp_path: Path) -> None:
    files = {"r10q.htm": b"inline", "r10q.xsd": b"schema", "R2.htm": b"render"}
    filing = _filing()
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    members = []
    for name, data in files.items():
        checksum = sha256(data).hexdigest()
        store.put_bytes(data, filing_object_key(filing, name, checksum))
        members.append(
            PackageMember(
                name=name,
                source_url=f"{filing.archive_directory_url}/{name}",
                size_bytes=len(data),
                checksum_sha256=checksum,
            )
        )
    old = FilingPackageManifest(
        filing=filing, members=tuple(members), acquired_at=datetime.now(UTC)
    )
    index_checksum = sha256(
        json.dumps(_index(*files), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    filing_checksum = sha256(
        json.dumps(filing.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    old_key = (
        f"manifests/sec/filings/{filing.accession}/index-sha256={index_checksum}/"
        f"filing-sha256={filing_checksum}.json"
    )
    store.put_bytes(old.model_dump_json().encode(), old_key)

    manifest = await FilingPackageDownloader(_settings(tmp_path), _Http(files), store).download_package(  # type: ignore[arg-type]
        filing
    )
    assert {member.name for member in manifest.members} == {"r10q.htm", "r10q.xsd"}
    assert store.exists(old_key), "previous manifests are immutable and must be retained"


# AC-7: archive fallback keeps the package safety limits.
@pytest.mark.parametrize(
    ("entries", "settings_update"),
    [
        ({"../escape.xml": b"x"}, {}),
        ({f"m{i}.xml": b"x" for i in range(5)}, {"filing_max_members": 3}),
        ({"bomb.xml": b"0" * 400_000}, {"zip_max_compression_ratio": 10}),
    ],
)
async def test_ac7_archive_fallback_enforces_limits(
    tmp_path: Path, entries: dict[str, bytes], settings_update: dict[str, object]
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    files = {f"{ACCESSION}.txt": b"x", f"{ACCESSION}-xbrl.zip": buffer.getvalue()}
    store = LocalObjectStore(tmp_path / "objects", "sec-xbrl")
    downloader = FilingPackageDownloader(
        _settings(tmp_path, **settings_update), _Http(files), store  # type: ignore[arg-type]
    )
    with pytest.raises(ArtifactIntegrityError):
        await downloader.download_package(_filing())
