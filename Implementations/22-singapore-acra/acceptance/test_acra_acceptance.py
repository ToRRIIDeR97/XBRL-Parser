"""Worker-visible acceptance tests for 22-singapore-acra."""

from __future__ import annotations

from datetime import date

from inbox_fixtures import EntityKeys, instance, row, write_inbox

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore


def acra_adapter(tmp_path, keys=None):
    from sec_xbrl_warehouse.sources.inbox import ACRA_PROFILE, InboxSourceAdapter

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox")
    return InboxSourceAdapter(settings, LocalObjectStore(tmp_path / "o", bucket="t"), keys or EntityKeys(), ACRA_PROFILE)


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


# AC-1: the ACRA profile keys issuers by UEN.
def test_ac1_profile() -> None:
    from sec_xbrl_warehouse.sources.inbox import ACRA_PROFILE

    assert ACRA_PROFILE.code == "acra"
    assert ACRA_PROFILE.entity_scheme == "https://www.acra.gov.sg/uen"
    assert ACRA_PROFILE.default_fiscal_period == "FY"


# AC-2: ACRA XBRL financial statements load through the inbox with UEN entity keys.
async def test_ac2_acra_rows(tmp_path) -> None:
    keys = EntityKeys()
    write_inbox(
        tmp_path / "inbox", "acra",
        [
            row("199901234A-FS-2024", "dbs-like.xml", entity_id="199901234A", company_name="Example Bank Ltd",
                form="FS", filed_date="2024-07-30", period_end="2024-06-30", fiscal_period=""),
            row("BAD-UEN-2024", "dbs-like.xml", entity_id="1999-01234", form="FS"),
        ],
        {"dbs-like.xml": instance()},
    )
    result = await acra_adapter(tmp_path, keys).discover(request())
    assert [f.accession for f in result.filings] == ["acra-199901234A-FS-2024"]
    filing = result.filings[0]
    assert (filing.fiscal_year, filing.fiscal_period, filing.report_date) == (2024, "FY", date(2024, 6, 30))
    assert list(keys.keys) == [("https://www.acra.gov.sg/uen", "199901234A")]


# AC-3: the stored package keeps ACRA's instance as the entrypoint.
async def test_ac3_package(tmp_path) -> None:
    write_inbox(tmp_path / "inbox", "acra",
                [row("199901234A-FS-2024", "fs.xml", entity_id="199901234A", form="FS")], {"fs.xml": instance()})
    source = acra_adapter(tmp_path)
    manifest = await source.download_package((await source.discover(request())).filings[0])
    assert manifest.package_object_key.startswith("bronze/acra/filings/")
    assert [m.name for m in manifest.members if m.is_xbrl_entrypoint] == ["fs.xml"]
