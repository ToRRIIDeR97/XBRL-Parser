"""Worker-visible acceptance tests for 19-esef-source."""

from __future__ import annotations

from datetime import date
from hashlib import sha256

import httpx
import pytest
from fxo_fixtures import HOST, LEI_A, LEI_B, EntityKeys, FakeFxo, report_package, two_page_feed

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore

FXO_A = f"{LEI_A}-2023-12-31-ESEF-GB-0"
FXO_B = f"{LEI_B}-2023-12-31-ESEF-GB-0"


def adapter_for(tmp_path, fake: FakeFxo, entity_keys: EntityKeys | None = None):
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
    from sec_xbrl_warehouse.sources.esef import ESEF_POLICY, EsefSourceAdapter

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "data")
    http = SourceHttpClient(settings, ESEF_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    store = LocalObjectStore(tmp_path / "objects", bucket="test")
    return EsefSourceAdapter(settings, http, store, entity_keys or EntityKeys()), store


def esef_bundle(*, report_date, facts=()):
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle

    filing = FilingRef(
        accession=f"esef-{FXO_A}", cik=10_000_000_001, company_name="Kainos", form="ESEF",
        filed_date=date(2024, 4, 10), report_date=report_date,
        archive_directory_url=f"https://{HOST}/{LEI_A}/2023-12-31/ESEF/GB/0",
    )
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="report.xhtml", document_type="inline", taxonomy_namespaces=(),
            source_documents=("report.xhtml",), software_name="arelle", software_version="test",
        ),
        contexts=(), concepts=(), facts=tuple(facts), relationships=(), footnotes=(), validation_issues=(),
    )


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


# AC-1: discovery pages newest-first, keeps the window, stops once past it.
async def test_ac1_discovery_window_and_mapping(tmp_path) -> None:
    pages, packages = two_page_feed()
    fake = FakeFxo(pages, packages)
    adapter, _ = adapter_for(tmp_path, fake)
    result = await adapter.discover(request(filed_from=date(2024, 4, 1), filed_to=date(2024, 4, 30)))

    assert [filing.accession for filing in result.filings] == [f"esef-{FXO_A}", f"esef-{FXO_B}"]
    first = result.filings[0]
    assert first.cik >= 10_000_000_000
    assert first.company_name == "KAINOS GROUP PLC"
    assert first.form == "ESEF"
    assert first.filed_date == date(2024, 4, 10)
    assert first.report_date == date(2023, 12, 31)
    assert first.is_inline_xbrl is True
    assert first.archive_directory_url == f"https://{HOST}/{LEI_A}/2023-12-31/ESEF/GB/0"
    assert {company.name for company in result.companies} == {"KAINOS GROUP PLC", "Example Holdings SE"}
    assert len(fake.requests) == 2  # page 2 reaches 2024-03-15, before the window: stop
    for sent in fake.requests:
        assert sent.url.params["sort"] == "-date_added"
        assert sent.url.params["include"] == "entity"


# AC-1: an LEI in the request keeps only that issuer's filings.
async def test_ac1_entity_filter(tmp_path) -> None:
    pages, packages = two_page_feed()
    adapter, _ = adapter_for(tmp_path, FakeFxo(pages, packages))
    result = await adapter.discover(
        request(filed_from=date(2024, 4, 1), filed_to=date(2024, 4, 30), entity=LEI_B)
    )
    assert [filing.accession for filing in result.filings] == [f"esef-{FXO_B}"]


# AC-2: the package is checked against the published SHA-256 and stored as one archive.
async def test_ac2_download_stores_the_report_package(tmp_path) -> None:
    from sec_xbrl_warehouse.ingestion.filing_downloader import filing_package_key

    pages, packages = two_page_feed()
    adapter, store = adapter_for(tmp_path, FakeFxo(pages, packages))
    filing = (await adapter.discover(request(filed_from=date(2024, 4, 1), filed_to=date(2024, 4, 30)))).filings[0]
    manifest = await adapter.download_package(filing)

    digest = sha256(report_package(LEI_A, "2023-12-31")).hexdigest()
    assert manifest.package_checksum_sha256 == digest
    assert manifest.package_object_key == filing_package_key(filing, digest)
    assert manifest.package_object_key.startswith(f"bronze/esef/filings/{filing.cik}/esef-{FXO_A}/")
    assert store.exists(manifest.package_object_key)
    entrypoints = [member.name for member in manifest.members if member.is_xbrl_entrypoint]
    assert len(entrypoints) == 1 and entrypoints[0].endswith(f"reports/{LEI_A}-2023-12-31-en.xhtml")


# AC-2: a package whose bytes don't match the published digest is rejected and not stored.
async def test_ac2_checksum_mismatch_is_rejected(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    pages, packages = two_page_feed()
    for path in packages:
        packages[path] = report_package(LEI_A, "1999-12-31")  # different bytes, same URL
    adapter, store = adapter_for(tmp_path, FakeFxo(pages, packages))
    filing = (await adapter.discover(request(filed_from=date(2024, 4, 1), filed_to=date(2024, 4, 30)))).filings[0]
    with pytest.raises(ArtifactIntegrityError):
        await adapter.download_package(filing)
    assert not list(store.list_objects("bronze/esef/"))


# AC-3: ESEF reports are annual; the fiscal year is the reporting period's end year.
def test_ac3_fiscal_focus_is_annual(tmp_path) -> None:
    adapter, _ = adapter_for(tmp_path, FakeFxo([], {}))
    enriched, _ = adapter.fiscal_focus(esef_bundle(report_date=date(2023, 12, 31)))
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2023, "FY")


# AC-4: the ESEF client talks only to filings.xbrl.org and has its own lock.
def test_ac4_policy() -> None:
    from sec_xbrl_warehouse.infrastructure.http.egress_lock import lock_name
    from sec_xbrl_warehouse.sources.esef import ESEF_POLICY, LEI_SCHEME

    assert ESEF_POLICY.code == "esef"
    assert ESEF_POLICY.allowed_hosts == frozenset({HOST})
    assert ESEF_POLICY.max_requests_per_second <= 2
    assert lock_name("esef") == "xbrl:esef-egress"
    assert LEI_SCHEME == "http://standards.iso.org/iso/17442"
