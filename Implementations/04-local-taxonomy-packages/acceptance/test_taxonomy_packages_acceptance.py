"""Worker-visible acceptance tests for 04-local-taxonomy-packages."""

from __future__ import annotations

import shutil
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PACKAGE = FIXTURES / "fixture-taxonomy-2025.zip"
PACKAGE_URL = "https://xbrl.fasb.org/fixture-taxonomy/2025/fixture-taxonomy-2025.zip"
REPO_CONFIG = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "config"


def _registry(config_dir: Path, *, sha: str, url: str = PACKAGE_URL, enabled: bool = True) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    for path in REPO_CONFIG.iterdir():
        if path.is_file():
            shutil.copy(path, config_dir / path.name)
    (config_dir / "taxonomy_packages.yml").write_text(
        "packages:\n"
        "  - id: fixture-2025\n"
        "    filename: fixture-taxonomy-2025.zip\n"
        f"    url: {url}\n"
        f"    sha256: {sha}\n"
        f"    enabled: {str(enabled).lower()}\n",
        encoding="utf-8",
    )


def _settings(tmp_path: Path, **updates: object) -> Settings:
    return Settings(
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        config_dir=tmp_path / "config",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=tmp_path / "packages",
        **updates,
    )


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


class _Http:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def download(self, url: str, destination: Path, **kwargs: object):
        self.urls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return destination, sha256(self.payload).hexdigest(), len(self.payload), {}


# AC-1: a registered, checksum-verified package works in offline mode.
@pytest.mark.arelle
def test_ac1_offline_extraction_resolves_through_registered_package(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    (tmp_path / "packages").mkdir()
    shutil.copy(PACKAGE, tmp_path / "packages" / PACKAGE.name)
    _registry(tmp_path / "config", sha=sha256(PACKAGE.read_bytes()).hexdigest())
    shutil.copytree(FIXTURES / "filing", tmp_path / "filing")

    bundle = ArelleExtractor(_settings(tmp_path, arelle_offline=True)).extract(
        tmp_path / "filing" / "example-20251231.xml", _filing()
    )

    revenue = next(fact for fact in bundle.facts if fact.concept_name == "Revenues")
    assert revenue.value_decimal == 1_234_000_000
    assert not [i for i in bundle.validation_issues if "xml.xsd" in i.message]


# AC-3: sync downloads a missing package, verifies it, and reports it ready.
async def test_ac3_sync_downloads_and_verifies_missing_package(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import (
        package_status,
        sync_taxonomy_packages,
    )

    payload = PACKAGE.read_bytes()
    _registry(tmp_path / "config", sha=sha256(payload).hexdigest())
    settings = _settings(tmp_path)
    assert [status.state for status in package_status(settings)] == ["missing"]

    http = _Http(payload)
    result = await sync_taxonomy_packages(settings, http)

    assert http.urls == [PACKAGE_URL]
    assert [status.state for status in result] == ["ok"]
    assert (tmp_path / "packages" / PACKAGE.name).read_bytes() == payload
    assert [status.state for status in package_status(settings)] == ["ok"]


# AC-3: a checksum mismatch never leaves a package file behind.
async def test_ac3_sync_rejects_checksum_mismatch(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import sync_taxonomy_packages

    _registry(tmp_path / "config", sha="0" * 64)
    with pytest.raises(ArtifactIntegrityError):
        await sync_taxonomy_packages(_settings(tmp_path), _Http(PACKAGE.read_bytes()))
    assert not (tmp_path / "packages" / PACKAGE.name).exists()
