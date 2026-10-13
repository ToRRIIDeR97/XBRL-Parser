"""Independent verification tests for 21-file-inbox-india."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from inbox_fixtures import EntityKeys, instance, row, write_inbox, zipped  # noqa: E402

from sec_xbrl_warehouse.config import Settings  # noqa: E402
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore  # noqa: E402


def adapter(tmp_path, profile_name="NSE_PROFILE", **values):
    from sec_xbrl_warehouse.sources import inbox

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "data", inbox_dir=tmp_path / "inbox", **values)
    return inbox.InboxSourceAdapter(
        settings, LocalObjectStore(tmp_path / "o", bucket="t"), EntityKeys(), getattr(inbox, profile_name)
    )


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


# AC-1: duplicate ids are an operator error, not a silent overwrite.
async def test_ac1_duplicate_native_ids(tmp_path) -> None:
    write_inbox(tmp_path / "inbox", "nse", [row("DUP-1", "a.xml"), row("DUP-1", "b.xml")],
                {"a.xml": instance(), "b.xml": instance()})
    with pytest.raises(ValueError, match="DUP-1"):
        await adapter(tmp_path).discover(request())


# AC-1: a missing required column is named in the error.
async def test_ac1_missing_column(tmp_path) -> None:
    inbox = tmp_path / "inbox" / "nse"
    inbox.mkdir(parents=True)
    (inbox / "index.csv").write_text("native_id,company_name,file\nX-1,Co,a.xml\n", encoding="utf-8")
    with pytest.raises(ValueError, match="filed_date"):
        await adapter(tmp_path).discover(request())


# AC-1: no inbox folder or an empty index is an empty result, not an error.
async def test_ac1_empty_states(tmp_path) -> None:
    assert (await adapter(tmp_path).discover(request())).filings == ()
    write_inbox(tmp_path / "inbox", "nse", [], {})
    assert (await adapter(tmp_path).discover(request())).filings == ()


# AC-1: entity and form filters; rows with bad dates or ids are skipped, not fatal.
async def test_ac1_filters_and_bad_rows(tmp_path) -> None:
    write_inbox(
        tmp_path / "inbox", "nse",
        [
            row("A-1", "a.xml"),
            row("B-1", "a.xml", entity_id="INE467B01029", form="shareholding"),
            row("C-1", "a.xml", filed_date="10/05/2024"),
            row("D 1", "a.xml"),
            row("E-1", "a.xml", entity_id="NOT-AN-ISIN"),
        ],
        {"a.xml": instance()},
    )
    source = adapter(tmp_path)
    assert [f.accession for f in (await source.discover(request())).filings] == ["nse-A-1", "nse-B-1"]
    assert [f.accession for f in (await source.discover(request(entity="INE467B01029"))).filings] == ["nse-B-1"]
    assert [f.accession for f in (await source.discover(request(forms=("results",)))).filings] == ["nse-A-1"]


# AC-1: without a fiscal period the profile default (FY) and the period-end year apply.
async def test_ac1_fiscal_defaults(tmp_path) -> None:
    write_inbox(tmp_path / "inbox", "nse", [row("A-1", "a.xml", fiscal_period="", period_end="2025-03-31")],
                {"a.xml": instance()})
    source = adapter(tmp_path)
    filing = (await source.discover(request(filed_from=date(2024, 1, 1), filed_to=date(2024, 12, 31)))).filings[0]
    assert (filing.fiscal_year, filing.fiscal_period) == (2025, "FY")


# AC-3: a symlink that leaves the inbox is refused even though its name looks local.
async def test_ac3_symlink_escape(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    secret = tmp_path / "secret.xml"
    secret.write_bytes(instance("secret"))
    write_inbox(tmp_path / "inbox", "nse", [row("A-1", "link.xml")], {})
    (tmp_path / "inbox" / "nse" / "link.xml").symlink_to(secret)
    source = adapter(tmp_path)
    with pytest.raises(ArtifactIntegrityError):
        await source.download_package((await source.discover(request())).filings[0])


# AC-3: size limit, missing file, two instances in one zip, and a non-XBRL single file.
@pytest.mark.parametrize(
    ("file", "data"),
    [
        ("big.xml", b"<x>" + b"a" * 4096 + b"</x>"),
        ("two.zip", zipped({"a.xbrl": instance(), "b.xbrl": instance()})),
        ("notes.txt", b"plain text"),
    ],
)
async def test_ac3_package_rules(tmp_path, file, data) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    write_inbox(tmp_path / "inbox", "nse", [row("A-1", file)], {file: data})
    source = adapter(tmp_path, filing_max_bytes=1024)
    with pytest.raises(ArtifactIntegrityError):
        await source.download_package((await source.discover(request())).filings[0])


async def test_ac3_missing_file(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    write_inbox(tmp_path / "inbox", "nse", [row("A-1", "gone.xml")], {})
    source = adapter(tmp_path)
    with pytest.raises(ArtifactIntegrityError):
        await source.download_package((await source.discover(request())).filings[0])


# AC-2: a later edit to the inbox file doesn't change the stored package for the same filing.
async def test_ac2_stored_bytes_are_immutable(tmp_path) -> None:
    write_inbox(tmp_path / "inbox", "nse", [row("A-1", "a.xml")], {"a.xml": instance("v1")})
    source = adapter(tmp_path)
    filing = (await source.discover(request())).filings[0]
    first = await source.download_package(filing)
    (tmp_path / "inbox" / "nse" / "a.xml").write_bytes(instance("v2"))
    second = await source.download_package(filing)
    assert first.package_object_key != second.package_object_key  # new bytes, new content address
    assert first.package_checksum_sha256 != second.package_checksum_sha256
