"""Worker-visible acceptance tests for 21-file-inbox-india."""

from __future__ import annotations

from datetime import date

import pytest
from inbox_fixtures import EntityKeys, digest, instance, row, write_inbox, zipped

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore


def adapter(tmp_path, profile_name: str = "NSE_PROFILE", keys: EntityKeys | None = None):
    from sec_xbrl_warehouse.sources import inbox

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox")
    store = LocalObjectStore(tmp_path / "objects", bucket="t")
    profile = getattr(inbox, profile_name)
    return inbox.InboxSourceAdapter(settings, store, keys or EntityKeys(), profile), store


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


# AC-1: the NSE inbox index becomes filings with ISIN entity keys and the row's fiscal fields.
async def test_ac1_index_rows_become_filings(tmp_path) -> None:
    from sec_xbrl_warehouse.sources.inbox import NSE_PROFILE

    write_inbox(
        tmp_path / "inbox", "nse",
        [
            row("RELIANCE-2024-Q4", "reliance-q4.xml"),
            row("TCS-2024-Q4", "tcs-q4.xml", entity_id="INE467B01029", company_name="Tata Consultancy Services",
                filed_date="2024-04-12", fiscal_period="Q4"),
            row("OLD-2023", "old.xml", filed_date="2023-05-10"),
        ],
        {"reliance-q4.xml": instance(), "tcs-q4.xml": instance(), "old.xml": instance()},
    )
    keys = EntityKeys()
    source, _ = adapter(tmp_path, keys=keys)
    result = await source.discover(request(filed_from=date(2024, 1, 1), filed_to=date(2024, 12, 31)))

    assert [f.accession for f in result.filings] == ["nse-RELIANCE-2024-Q4", "nse-TCS-2024-Q4"]
    reliance = result.filings[0]
    assert (reliance.form, reliance.filed_date, reliance.report_date) == ("results", date(2024, 5, 10), date(2024, 3, 31))
    assert (reliance.fiscal_year, reliance.fiscal_period) == (2024, "FY")
    assert result.filings[1].fiscal_period == "Q4"
    assert (NSE_PROFILE.entity_scheme, "INE002A01018") in keys.keys
    assert {c.name for c in result.companies} == {"Reliance Industries Limited", "Tata Consultancy Services"}


# AC-2: a single instance file is archived as a one-member package and becomes the entrypoint.
async def test_ac2_single_file_package(tmp_path) -> None:
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_package_key

    data = instance("reliance")
    write_inbox(tmp_path / "inbox", "nse", [row("RELIANCE-2024-Q4", "reliance-q4.xml", sha256=digest(data))],
                {"reliance-q4.xml": data})
    source, store = adapter(tmp_path)
    filing = (await source.discover(request())).filings[0]
    manifest = await source.download_package(filing)
    assert [m.name for m in manifest.members if m.is_xbrl_entrypoint] == ["reliance-q4.xml"]
    assert manifest.package_object_key == filing_package_key(filing, manifest.package_checksum_sha256)
    assert store.exists(manifest.package_object_key)
    assert (tmp_path / "inbox" / "nse" / "reliance-q4.xml").read_bytes() == data  # inbox untouched


# AC-2: a zip with one instance keeps its members; the instance is the entrypoint.
async def test_ac2_zip_package(tmp_path) -> None:
    package = zipped({"filing/instance.xbrl": instance(), "filing/schema.xsd": b"<schema/>"})
    write_inbox(tmp_path / "inbox", "mca", [row("U12345MH2000PLC123456-2024", "aoc4.zip", entity_id="U12345MH2000PLC123456")],
                {"aoc4.zip": package})
    source, _ = adapter(tmp_path, "MCA_PROFILE")
    manifest = await source.download_package((await source.discover(request())).filings[0])
    assert [m.name for m in manifest.members if m.is_xbrl_entrypoint] == ["filing/instance.xbrl"]
    assert {m.name for m in manifest.members} == {"filing/instance.xbrl", "filing/schema.xsd"}


# AC-3: files outside the inbox and digest mismatches are refused.
@pytest.mark.parametrize(
    ("file", "sha"),
    [("../outside.xml", ""), ("/etc/hosts", ""), ("reliance-q4.xml", "0" * 64)],
)
async def test_ac3_unsafe_or_mismatched_files_are_refused(tmp_path, file, sha) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "outside.xml").write_bytes(instance())
    write_inbox(tmp_path / "inbox", "nse", [row("RELIANCE-2024-Q4", file, sha256=sha)], {"reliance-q4.xml": instance()})
    source, store = adapter(tmp_path)
    filing = (await source.discover(request())).filings[0]
    with pytest.raises(ArtifactIntegrityError):
        await source.download_package(filing)
    assert not list(store.list_objects("bronze/"))


# AC-4: the inbox source makes no network requests at all.
def test_ac4_no_network_policy() -> None:
    from sec_xbrl_warehouse.sources.inbox import MCA_PROFILE, NSE_PROFILE

    assert (NSE_PROFILE.code, MCA_PROFILE.code) == ("nse", "mca")
    assert NSE_PROFILE.entity_scheme == "urn:isin"
    assert MCA_PROFILE.entity_scheme == "https://www.mca.gov.in/cin"


# AC-5: `inbox check` reports index problems without enqueuing anything.
def test_ac5_inbox_check_cli(tmp_path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from sec_xbrl_warehouse.cli import app
    from sec_xbrl_warehouse.config import get_settings

    write_inbox(tmp_path / "inbox", "nse",
                [row("GOOD-1", "good.xml"), row("BAD 2", "good.xml"), row("MISSING-3", "absent.xml")],
                {"good.xml": instance()})
    monkeypatch.setenv("XBRL_INBOX_DIR", str(tmp_path / "inbox"))
    monkeypatch.setenv("XBRL_ENV", "test")
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["inbox", "check", "--source", "nse"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1, result.output
    assert "BAD 2" in result.output and "absent.xml" in result.output
    assert "1 valid, 2 problems" in result.output
