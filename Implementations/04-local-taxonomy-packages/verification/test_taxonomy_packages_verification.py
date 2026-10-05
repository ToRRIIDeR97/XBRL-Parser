"""Independent verification tests for 04-local-taxonomy-packages."""

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
REPO_CONFIG = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "config"


def _registry(config_dir: Path, entries: list[dict[str, object]]) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    for path in REPO_CONFIG.iterdir():
        if path.is_file():
            shutil.copy(path, config_dir / path.name)
    lines = ["packages:"]
    for entry in entries:
        lines.append(f"  - id: {entry['id']}")
        for key in ("filename", "url", "sha256", "enabled"):
            if key in entry:
                value = entry[key]
                lines.append(f"    {key}: {str(value).lower() if isinstance(value, bool) else value}")
    (config_dir / "taxonomy_packages.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


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


class _Http:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    async def download(self, url: str, destination: Path, **kwargs: object):
        self.urls.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.payload)
        return destination, sha256(self.payload).hexdigest(), len(self.payload), {}


def _entry(**updates: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "fixture-2025",
        "filename": PACKAGE.name,
        "url": "https://xbrl.fasb.org/fixture-taxonomy/2025/fixture-taxonomy-2025.zip",
        "sha256": sha256(PACKAGE.read_bytes()).hexdigest(),
        "enabled": True,
    }
    entry.update(updates)
    return entry


# AC-2: offline mode still fails fast, naming the missing taxonomy (existing behavior).
@pytest.mark.arelle
def test_ac2_offline_without_package_fails_fast_naming_url(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.exceptions import XbrlExtractionError
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    _registry(tmp_path / "config", [])
    (tmp_path / "config" / "taxonomy_packages.yml").write_text("packages: []\n", encoding="utf-8")
    shutil.copytree(FIXTURES / "filing", tmp_path / "filing")
    filing = FilingRef(
        accession="0000000004-26-000004",
        cik=4,
        company_name="Verify Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/4/4",
    )
    with pytest.raises(XbrlExtractionError, match="fixture-taxonomy/2025/base.xsd"):
        ArelleExtractor(_settings(tmp_path, arelle_offline=True)).extract(
            tmp_path / "filing" / "example-20251231.xml", filing
        )


# AC-3: an already verified package is not downloaded again (restart / re-run).
async def test_ac3_sync_is_idempotent_for_verified_packages(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import sync_taxonomy_packages

    (tmp_path / "packages").mkdir()
    shutil.copy(PACKAGE, tmp_path / "packages" / PACKAGE.name)
    _registry(tmp_path / "config", [_entry()])
    http = _Http(PACKAGE.read_bytes())
    result = await sync_taxonomy_packages(_settings(tmp_path), http)
    assert http.urls == []
    assert [status.state for status in result] == ["ok"]


# AC-3: only HTTPS URLs on the taxonomy allowlist are fetched.
@pytest.mark.parametrize(
    "url",
    [
        "http://xbrl.fasb.org/fixture-taxonomy-2025.zip",
        "https://example.net/fixture-taxonomy-2025.zip",
        "https://user:pw@xbrl.fasb.org/fixture-taxonomy-2025.zip",
    ],
)
async def test_ac3_sync_refuses_urls_outside_policy(tmp_path: Path, url: str) -> None:
    from sec_xbrl_warehouse.exceptions import ConfigurationError
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import sync_taxonomy_packages

    _registry(tmp_path / "config", [_entry(url=url)])
    http = _Http(PACKAGE.read_bytes())
    with pytest.raises(ConfigurationError):
        await sync_taxonomy_packages(_settings(tmp_path), http)
    assert http.urls == []


# AC-4: status distinguishes corrupt, disabled and unsafe entries without network access.
def test_ac4_status_reports_mismatch_disabled_and_unsafe_filename(tmp_path: Path) -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.taxonomy_packages import package_status

    (tmp_path / "packages").mkdir()
    (tmp_path / "packages" / "corrupt.zip").write_bytes(b"not the pinned bytes")
    _registry(
        tmp_path / "config",
        [
            _entry(id="corrupt", filename="corrupt.zip"),
            _entry(id="off", filename="off.zip", enabled=False),
            _entry(id="escape", filename="../escape.zip"),
        ],
    )
    states = {status.package_id: status.state for status in package_status(_settings(tmp_path))}
    assert states == {"corrupt": "checksum_mismatch", "off": "disabled", "escape": "unsafe_path"}


# AC-5: the shipped registry pins real packages with URL and checksum for every entry.
def test_ac5_repository_registry_is_populated_and_pinned() -> None:
    from sec_xbrl_warehouse.config_files import load_yaml

    packages = load_yaml(REPO_CONFIG / "taxonomy_packages.yml").get("packages", [])
    ids = {str(package["id"]) for package in packages}
    assert any(package_id.startswith("us-gaap-") for package_id in ids)
    assert any(package_id.startswith("dei-") for package_id in ids)
    assert any(package_id.startswith("srt-") for package_id in ids)
    for package in packages:
        assert str(package["url"]).startswith("https://"), package
        assert len(str(package["sha256"])) == 64, package
