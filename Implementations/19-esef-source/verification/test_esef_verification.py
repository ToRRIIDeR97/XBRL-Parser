"""Independent verification tests for 19-esef-source."""

from __future__ import annotations

import io
import sys
import zipfile
from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from fxo_fixtures import (  # noqa: E402
    HOST,
    LEI_A,
    EntityKeys,
    FakeFxo,
    entity_record,
    filing_record,
)

from sec_xbrl_warehouse.config import Settings  # noqa: E402
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore  # noqa: E402


def adapter_for(tmp_path, fake, entity_keys=None, **settings_values):
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
    from sec_xbrl_warehouse.sources.esef import ESEF_POLICY, EsefSourceAdapter

    settings = Settings(_env_file=None, env="test", data_dir=tmp_path / "data", **settings_values)
    http = SourceHttpClient(settings, ESEF_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return EsefSourceAdapter(settings, http, LocalObjectStore(tmp_path / "o", bucket="t"), entity_keys or EntityKeys())


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


def single(fxo_id: str, package: bytes, *, lei: str = LEI_A, digest: str | None = None) -> FakeFxo:
    record = filing_record(
        fxo_id, lei, added="2024-04-10", period_end="2023-12-31",
        digest=digest if digest is not None else sha256(package).hexdigest(), record_id="1",
    )
    page = {"data": [record], "included": [entity_record(lei, "Issuer")], "links": {}}
    return FakeFxo([page], {record["attributes"]["package_url"]: package})


def zipped(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


async def first_filing(adapter):
    return (await adapter.discover(request())).filings[0]


# AC-1: an endless `next` chain stops at the configured page cap.
async def test_ac1_page_cap(tmp_path) -> None:
    page = {
        "data": [filing_record(f"{LEI_A}-2023-12-31-ESEF-GB-0", LEI_A, added="2024-04-10",
                               period_end="2023-12-31", digest="0" * 64, record_id="1")],
        "included": [entity_record(LEI_A, "Issuer")],
        "links": {"next": "https://filings.xbrl.org/api/filings?page[number]=1"},
    }

    class Endless(FakeFxo):
        def __call__(self, request):
            self.requests.append(request)
            return httpx.Response(200, json=page)

    fake = Endless([], {})
    adapter = adapter_for(tmp_path, fake, esef_max_pages=3)
    await adapter.discover(request())
    assert len(fake.requests) == 3


# AC-1: a record whose id can't form a filing key is skipped; the rest still load.
async def test_ac1_unsafe_record_is_skipped(tmp_path) -> None:
    good = filing_record(f"{LEI_A}-2023-12-31-ESEF-GB-0", LEI_A, added="2024-04-10",
                         period_end="2023-12-31", digest="0" * 64, record_id="2")
    bad = filing_record("../../etc/passwd", LEI_A, added="2024-04-10",
                        period_end="2023-12-31", digest="0" * 64, record_id="3")
    fake = FakeFxo([{"data": [bad, good], "included": [entity_record(LEI_A, "Issuer")], "links": {}}], {})
    result = await adapter_for(tmp_path, fake).discover(request())
    assert [filing.accession for filing in result.filings] == [f"esef-{LEI_A}-2023-12-31-ESEF-GB-0"]


# AC-1: one LEI gets one entity key across filings, under the LEI scheme.
async def test_ac1_one_key_per_lei(tmp_path) -> None:
    records = [
        filing_record(f"{LEI_A}-2023-12-31-ESEF-GB-{n}", LEI_A, added="2024-04-10",
                      period_end="2023-12-31", digest="0" * 64, record_id=str(n))
        for n in range(3)
    ]
    keys = EntityKeys()
    fake = FakeFxo([{"data": records, "included": [entity_record(LEI_A, "Issuer")], "links": {}}], {})
    result = await adapter_for(tmp_path, fake, keys).discover(request())
    assert len({filing.cik for filing in result.filings}) == 1
    assert list(keys.keys) == [("http://standards.iso.org/iso/17442", LEI_A)]
    assert len(result.companies) == 1


# AC-2: archive safety: traversal names, non-zip bytes, member limits.
@pytest.mark.parametrize(
    "package",
    [
        zipped({"../escape/reports/a.xhtml": b"<html/>"}),
        b"not a zip at all",
        zipped({f"root/reports/r{n}.xhtml": b"<html/>" for n in range(30)}),
    ],
)
async def test_ac2_unsafe_packages_are_rejected(tmp_path, package) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    fake = single(f"{LEI_A}-2023-12-31-ESEF-GB-0", package)
    adapter = adapter_for(tmp_path, fake, filing_max_members=10)
    with pytest.raises(ArtifactIntegrityError):
        await adapter.download_package(await first_filing(adapter))


# AC-2: a package without any inline XBRL report has no entrypoint and is rejected.
async def test_ac2_package_without_report_is_rejected(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    fake = single(f"{LEI_A}-2023-12-31-ESEF-GB-0", zipped({"root/META-INF/taxonomyPackage.xml": b"<x/>"}))
    adapter = adapter_for(tmp_path, fake)
    with pytest.raises(ArtifactIntegrityError):
        await adapter.download_package(await first_filing(adapter))


# AC-2: a re-download of the same bytes is idempotent (same key, one object).
async def test_ac2_redownload_is_idempotent(tmp_path) -> None:
    package = zipped({"root/reports/report.xhtml": b"<html/>"})
    fake = single(f"{LEI_A}-2023-12-31-ESEF-GB-0", package)
    adapter = adapter_for(tmp_path, fake)
    filing = await first_filing(adapter)
    first = await adapter.download_package(filing)
    second = await adapter.download_package(filing)
    assert first.package_object_key == second.package_object_key
    assert all(request.url.host == HOST for request in fake.requests)


# AC-3: without a report date, the year comes from the latest annual duration fact.
def test_ac3_fiscal_year_from_facts(tmp_path) -> None:
    from hashlib import sha256 as h

    from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact, XbrlUnit

    def fact(start, end, value):
        digest = h(f"{start}{end}{value}".encode()).hexdigest()
        return XbrlFact(
            fact_id=digest, concept_namespace="https://xbrl.ifrs.org/taxonomy/2023-03-23/ifrs-full",
            concept_name="Revenue", concept_qname="ifrs-full:Revenue", context_id="c", context_hash=digest,
            unit=XbrlUnit(unit_id="u", numerator_measures=("iso4217:GBP",)), value_kind=FactValueKind.NUMERIC,
            raw_value=str(value), value_decimal=Decimal(value), period_kind=PeriodKind.DURATION,
            period_start=start, period_end=end, entity_scheme="http://standards.iso.org/iso/17442",
            entity_identifier=LEI_A, dimensions=(), dimension_hash=digest, duplicate_set_hash=digest,
            source_document="report.xhtml",
        )

    bundle = XbrlExtractionBundle(
        filing=FilingRef(
            accession=f"esef-{LEI_A}-2024-03-31-ESEF-GB-0", cik=10_000_000_001, company_name="Issuer",
            form="ESEF", filed_date=date(2024, 7, 1), archive_directory_url=f"https://{HOST}/x",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="report.xhtml", document_type="inline", taxonomy_namespaces=(),
            source_documents=("report.xhtml",), software_name="arelle", software_version="test",
        ),
        contexts=(), concepts=(),
        facts=(fact(date(2022, 4, 1), date(2023, 3, 31), 1), fact(date(2023, 4, 1), date(2024, 3, 31), 2)),
        relationships=(), footnotes=(), validation_issues=(),
    )
    adapter = adapter_for(tmp_path, FakeFxo([], {}))
    enriched, _ = adapter.fiscal_focus(bundle)
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2024, "FY")
