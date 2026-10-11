"""Worker-visible acceptance tests for 20-edinet-dart-sources."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from api_fixtures import (
    DART_HOST,
    DART_KEY,
    EDINET_HOST,
    EDINET_KEY,
    EntityKeys,
    FakeDart,
    FakeEdinet,
    dart_item,
    dart_package,
    edinet_doc,
    edinet_package,
)

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore


def settings(tmp_path, **values) -> Settings:
    return Settings(
        _env_file=None, env="test", data_dir=tmp_path / "data",
        edinet_api_key=EDINET_KEY, dart_api_key=DART_KEY, **values,
    )


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


def edinet(tmp_path, fake, **values):
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
    from sec_xbrl_warehouse.sources.edinet import EDINET_POLICY, EdinetSourceAdapter

    config = settings(tmp_path, **values)
    http = SourceHttpClient(config, EDINET_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    store = LocalObjectStore(tmp_path / "o", bucket="t")
    return EdinetSourceAdapter(config, http, store, EntityKeys()), store


def dart(tmp_path, fake, **values):
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
    from sec_xbrl_warehouse.sources.dart import DART_POLICY, DartSourceAdapter

    config = settings(tmp_path, **values)
    http = SourceHttpClient(config, DART_POLICY, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    store = LocalObjectStore(tmp_path / "o", bucket="t")
    return DartSourceAdapter(config, http, store, EntityKeys()), store


# AC-1: EDINET discovery reads one document list per day and keeps XBRL periodic reports.
async def test_ac1_edinet_discovery(tmp_path) -> None:
    fake = FakeEdinet(
        {
            "2024-06-25": [edinet_doc("S100TR7I"), edinet_doc("S100XXXX", xbrl="0"), edinet_doc("S100YYYY", doc_type="350")],
            "2024-06-26": [edinet_doc("S100ZZZZ", doc_type="140", submitted="2024-06-26 09:30")],
        }
    )
    adapter, _ = edinet(tmp_path, fake)
    result = await adapter.discover(request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 26)))

    assert [filing.accession for filing in result.filings] == ["edinet-S100TR7I", "edinet-S100ZZZZ"]
    first = result.filings[0]
    assert first.form == "120"
    assert first.filed_date == date(2024, 6, 25)
    assert first.report_date == date(2024, 3, 31)
    assert first.company_name == "トヨタ自動車株式会社"
    assert first.cik >= 10_000_000_000
    assert [r.url.params["date"] for r in fake.requests] == ["2024-06-25", "2024-06-26"]
    assert all(r.url.params["type"] == "2" for r in fake.requests)


# AC-2: the EDINET package keeps the public-document instance as its entrypoint.
async def test_ac2_edinet_download(tmp_path) -> None:
    fake = FakeEdinet({"2024-06-25": [edinet_doc("S100TR7I")]}, {"S100TR7I": edinet_package("S100TR7I")})
    adapter, store = edinet(tmp_path, fake)
    filing = (await adapter.discover(request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25)))).filings[0]
    manifest = await adapter.download_package(filing)
    entrypoints = [m.name for m in manifest.members if m.is_xbrl_entrypoint]
    assert len(entrypoints) == 1 and entrypoints[0].startswith("XBRL/PublicDoc/") and entrypoints[0].endswith(".xbrl")
    assert store.exists(manifest.package_object_key)
    download = fake.requests[-1]
    assert download.url.path == "/api/v2/documents/S100TR7I" and download.url.params["type"] == "1"


# AC-3: API keys never leave the request: not in filings, manifests, object keys or errors.
async def test_ac3_keys_stay_secret(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError

    fake = FakeEdinet({"2024-06-25": [edinet_doc("S100TR7I")]}, {"S100TR7I": edinet_package("S100TR7I")})
    adapter, store = edinet(tmp_path, fake)
    filing = (await adapter.discover(request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25)))).filings[0]
    manifest = await adapter.download_package(filing)
    assert EDINET_KEY not in filing.model_dump_json()
    assert EDINET_KEY not in manifest.model_dump_json()
    assert all(EDINET_KEY not in key for key, _ in store.list_objects(""))

    denied, _ = edinet(tmp_path / "x", FakeEdinet({}, status="401"))
    with pytest.raises(SourceAccessError) as raised:
        await denied.discover(request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25)))
    assert EDINET_KEY not in str(raised.value)


# AC-4: DART discovery maps periodic reports to report codes and amendment flags.
async def test_ac4_dart_discovery(tmp_path) -> None:
    fake = FakeDart(
        [[
            dart_item("20240312000736", "사업보고서 (2023.12)"),
            dart_item("20240515001234", "분기보고서 (2024.03)", date="20240515"),
            dart_item("20240601000001", "[기재정정]반기보고서 (2023.06)", date="20240601"),
            dart_item("20240312009999", "주요사항보고서(자기주식취득결정)"),
        ]]
    )
    adapter, _ = dart(tmp_path, fake)
    result = await adapter.discover(request(filed_from=date(2024, 3, 1), filed_to=date(2024, 6, 30)))
    summary = [(f.accession, f.form, f.report_date, f.is_amendment) for f in result.filings]
    assert summary == [
        ("dart-20240312000736", "11011", date(2023, 12, 31), False),
        ("dart-20240515001234", "11013", date(2024, 3, 31), False),
        ("dart-20240601000001", "11012", date(2023, 6, 30), True),
    ]
    sent = fake.requests[0]
    assert sent.url.params["bgn_de"] == "20240301" and sent.url.params["end_de"] == "20240630"
    assert sent.url.params["pblntf_ty"] == "A"


# AC-4: no data is an empty result; the daily limit is a retryable deferral.
async def test_ac4_dart_status_codes(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import SourceRateLimited
    from sec_xbrl_warehouse.worker import is_retryable_exception

    empty, _ = dart(tmp_path, FakeDart([[]], status="013"))
    assert (await empty.discover(request(filed_from=date(2024, 3, 1), filed_to=date(2024, 3, 2)))).filings == ()
    limited, _ = dart(tmp_path / "l", FakeDart([[]], status="020"))
    with pytest.raises(SourceRateLimited) as raised:
        await limited.discover(request(filed_from=date(2024, 3, 1), filed_to=date(2024, 3, 2)))
    assert is_retryable_exception(raised.value)


# AC-5: DART downloads the original XBRL zip for the report.
async def test_ac5_dart_download(tmp_path) -> None:
    fake = FakeDart([[dart_item("20240312000736", "사업보고서 (2023.12)")]], {"20240312000736": dart_package()})
    adapter, store = dart(tmp_path, fake)
    filing = (await adapter.discover(request(filed_from=date(2024, 3, 1), filed_to=date(2024, 3, 31)))).filings[0]
    manifest = await adapter.download_package(filing)
    assert [m.name for m in manifest.members if m.is_xbrl_entrypoint] == ["entity00126380_2023-12-31.xbrl"]
    download = fake.requests[-1]
    assert download.url.params["rcept_no"] == "20240312000736" and download.url.params["reprt_code"] == "11011"
    assert DART_KEY not in manifest.model_dump_json()


# AC-6: fiscal fields: EDINET from jpdei facts (doc type fallback), DART from the report code.
def test_ac6_fiscal_focus_fallbacks(tmp_path) -> None:
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle

    def bundle(key: str, form: str, report_date: date) -> XbrlExtractionBundle:
        return XbrlExtractionBundle(
            filing=FilingRef(accession=key, cik=10_000_000_001, company_name="X", form=form,
                             filed_date=date(2024, 6, 25), report_date=report_date,
                             archive_directory_url="https://example.invalid/x"),
            metadata=XbrlDocumentMetadata(entrypoint="a.xbrl", document_type="xbrl", taxonomy_namespaces=(),
                                          source_documents=("a.xbrl",), software_name="arelle", software_version="t"),
            contexts=(), concepts=(), facts=(), relationships=(), footnotes=(), validation_issues=(),
        )

    edinet_adapter, _ = edinet(tmp_path, FakeEdinet({}))
    enriched, _ = edinet_adapter.fiscal_focus(bundle("edinet-S100TR7I", "120", date(2024, 3, 31)))
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2024, "FY")
    dart_adapter, _ = dart(tmp_path, FakeDart([[]]))
    enriched, _ = dart_adapter.fiscal_focus(bundle("dart-20240515001234", "11013", date(2024, 3, 31)))
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2024, "Q1")


# AC-7: each source talks only to its own API host.
def test_ac7_policies() -> None:
    from sec_xbrl_warehouse.sources.dart import DART_POLICY
    from sec_xbrl_warehouse.sources.edinet import EDINET_POLICY

    assert (EDINET_POLICY.code, EDINET_POLICY.allowed_hosts) == ("edinet", frozenset({EDINET_HOST}))
    assert (DART_POLICY.code, DART_POLICY.allowed_hosts) == ("dart", frozenset({DART_HOST}))
    assert EDINET_POLICY.max_requests_per_second <= 1 and DART_POLICY.max_requests_per_second <= 1
