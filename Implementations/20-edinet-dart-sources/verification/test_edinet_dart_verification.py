"""Independent verification tests for 20-edinet-dart-sources."""

from __future__ import annotations

import sys
from datetime import date
from hashlib import sha256
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "acceptance"))
from api_fixtures import (  # noqa: E402
    DART_KEY,
    EDINET_KEY,
    EntityKeys,
    FakeDart,
    FakeEdinet,
    dart_item,
    edinet_doc,
    zipped,
)

from sec_xbrl_warehouse.config import Settings  # noqa: E402
from sec_xbrl_warehouse.infrastructure.storage.object_store import LocalObjectStore  # noqa: E402


def request(**values):
    from sec_xbrl_warehouse.sources.base import DiscoveryRequest

    return DiscoveryRequest(**{"filed_from": None, "filed_to": None, "entity": None, "forms": (), **values})


def build(kind, tmp_path, fake, **values):
    from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient

    if kind == "edinet":
        from sec_xbrl_warehouse.sources.edinet import EDINET_POLICY as policy
        from sec_xbrl_warehouse.sources.edinet import EdinetSourceAdapter as adapter_class
    else:
        from sec_xbrl_warehouse.sources.dart import DART_POLICY as policy
        from sec_xbrl_warehouse.sources.dart import DartSourceAdapter as adapter_class
    keys = {"edinet_api_key": EDINET_KEY, "dart_api_key": DART_KEY}
    keys.update(values)
    config = Settings(_env_file=None, env="test", data_dir=tmp_path / "d", **keys)
    http = SourceHttpClient(config, policy, client=httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    return adapter_class(config, http, LocalObjectStore(tmp_path / "o", bucket="t"), EntityKeys())


# AC-1: no key means no request; the window must be bounded and short.
async def test_ac1_edinet_requires_key_and_bounded_window(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import ConfigurationError

    fake = FakeEdinet({})
    with pytest.raises(ConfigurationError):
        await build("edinet", tmp_path, fake, edinet_api_key=None).discover(
            request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25))
        )
    adapter = build("edinet", tmp_path / "b", fake)
    with pytest.raises(ValueError):
        await adapter.discover(request(filed_from=date(2024, 1, 1), filed_to=date(2024, 6, 30)))
    with pytest.raises(ValueError):
        await adapter.discover(request(filed_to=date(2024, 6, 25)))
    assert fake.requests == []


# AC-1: withdrawn documents are skipped; amended reports are flagged; forms and entity filter.
async def test_ac1_edinet_withdrawn_amended_and_filters(tmp_path) -> None:
    fake = FakeEdinet({
        "2024-06-25": [
            edinet_doc("S1000001", withdrawn="1"),
            edinet_doc("S1000002", doc_type="130"),
            edinet_doc("S1000003", code="E99999"),
            edinet_doc("S1000004", doc_type="160"),
        ]
    })
    adapter = build("edinet", tmp_path, fake)
    day = {"filed_from": date(2024, 6, 25), "filed_to": date(2024, 6, 25)}
    everything = (await adapter.discover(request(**day))).filings
    assert [f.accession for f in everything] == ["edinet-S1000002", "edinet-S1000003", "edinet-S1000004"]
    assert [f.is_amendment for f in everything] == [True, False, False]
    only_e02144 = (await adapter.discover(request(**day, entity="E02144"))).filings
    assert [f.accession for f in only_e02144] == ["edinet-S1000002", "edinet-S1000004"]
    semiannual = (await adapter.discover(request(**day, forms=("160",)))).filings
    assert [f.accession for f in semiannual] == ["edinet-S1000004"]


# AC-2: a JSON error body served with status 200 is an access error, not a package.
async def test_ac2_edinet_json_body_is_not_a_package(tmp_path) -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError

    fake = FakeEdinet({"2024-06-25": [edinet_doc("S100GONE")]}, packages={})
    adapter = build("edinet", tmp_path, fake)
    filing = (await adapter.discover(request(filed_from=date(2024, 6, 25), filed_to=date(2024, 6, 25)))).filings[0]
    with pytest.raises(SourceAccessError) as raised:
        await adapter.download_package(filing)
    assert EDINET_KEY not in str(raised.value)


# AC-2/AC-5: a package without a public-document instance is rejected.
@pytest.mark.parametrize("kind", ["edinet", "dart"])
async def test_ac2_package_without_instance_is_rejected(tmp_path, kind) -> None:
    from sec_xbrl_warehouse.exceptions import ArtifactIntegrityError

    if kind == "edinet":
        bad = zipped({"XBRL/AuditDoc/only-audit.xbrl": b"<xbrl/>"})
        fake = FakeEdinet({"2024-06-25": [edinet_doc("S100BAD1")]}, {"S100BAD1": bad})
        window = {"filed_from": date(2024, 6, 25), "filed_to": date(2024, 6, 25)}
    else:
        bad = zipped({"readme.txt": b"no instance"})
        fake = FakeDart([[dart_item("20240312000736", "사업보고서 (2023.12)")]], {"20240312000736": bad})
        window = {"filed_from": date(2024, 3, 1), "filed_to": date(2024, 3, 31)}
    adapter = build(kind, tmp_path, fake)
    filing = (await adapter.discover(request(**window))).filings[0]
    with pytest.raises(ArtifactIntegrityError):
        await adapter.download_package(filing)


# AC-3: a failing request's error and logs carry no key (DART, error path).
async def test_ac3_dart_error_has_no_key(tmp_path, caplog) -> None:
    from sec_xbrl_warehouse.exceptions import SourceAccessError

    adapter = build("dart", tmp_path, FakeDart([[]], status="010"))
    with pytest.raises(SourceAccessError) as raised:
        await adapter.discover(request(filed_from=date(2024, 3, 1), filed_to=date(2024, 3, 2)))
    assert DART_KEY not in str(raised.value)
    assert DART_KEY not in caplog.text


# AC-4: DART pages through total_page; 3rd-quarter reports get 11014; entity filter is sent.
async def test_ac4_dart_paging_and_q3(tmp_path) -> None:
    fake = FakeDart([
        [dart_item("20241114000001", "분기보고서 (2024.09)", date="20241114")],
        [dart_item("20241114000002", "분기보고서 (2024.09)", corp_code="00164779", date="20241114")],
    ])
    adapter = build("dart", tmp_path, fake)
    filings = (await adapter.discover(request(filed_from=date(2024, 11, 1), filed_to=date(2024, 11, 30)))).filings
    assert [(f.accession, f.form, f.report_date) for f in filings] == [
        ("dart-20241114000001", "11014", date(2024, 9, 30)),
        ("dart-20241114000002", "11014", date(2024, 9, 30)),
    ]
    assert [r.url.params["page_no"] for r in fake.requests] == ["1", "2"]
    assert len({f.cik for f in filings}) == 2

    scoped = FakeDart([[dart_item("20241114000001", "분기보고서 (2024.09)", date="20241114")]])
    await build("dart", tmp_path / "s", scoped).discover(
        request(filed_from=date(2024, 11, 1), filed_to=date(2024, 11, 30), entity="00126380")
    )
    assert scoped.requests[0].url.params["corp_code"] == "00126380"


# AC-6: EDINET fiscal fields come from jpdei facts when present (half-year → H1).
def test_ac6_edinet_fiscal_from_jpdei(tmp_path) -> None:
    from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
    from sec_xbrl_warehouse.domain.models import FilingRef
    from sec_xbrl_warehouse.domain.xbrl import XbrlDocumentMetadata, XbrlExtractionBundle, XbrlFact

    jpdei = "http://disclosure.edinet-fsa.go.jp/taxonomy/jpdei/2013-08-31/jpdei_cor"

    def fact(name, kind, value):
        digest = sha256(name.encode()).hexdigest()
        return XbrlFact(
            fact_id=digest, concept_namespace=jpdei, concept_name=name, concept_qname=f"jpdei_cor:{name}",
            context_id="c", context_hash=digest, unit=None, value_kind=kind, raw_value=str(value),
            value_decimal=None, value_text=value if kind == FactValueKind.TEXT else None,
            value_date=value if kind == FactValueKind.DATE else None,
            period_kind=PeriodKind.DURATION, period_start=date(2024, 4, 1), period_end=date(2024, 9, 30),
            entity_scheme="http://disclosure.edinet-fsa.go.jp", entity_identifier="E02144-000",
            dimensions=(), dimension_hash=digest, duplicate_set_hash=digest, source_document="a.xbrl",
        )

    bundle = XbrlExtractionBundle(
        filing=FilingRef(accession="edinet-S100HALF", cik=10_000_000_001, company_name="X", form="160",
                         filed_date=date(2024, 11, 12), report_date=date(2024, 9, 30),
                         archive_directory_url="https://example.invalid/x"),
        metadata=XbrlDocumentMetadata(entrypoint="a.xbrl", document_type="xbrl", taxonomy_namespaces=(),
                                      source_documents=("a.xbrl",), software_name="arelle", software_version="t"),
        contexts=(), concepts=(),
        facts=(
            fact("CurrentFiscalYearEndDateDEI", FactValueKind.DATE, date(2025, 3, 31)),
            fact("TypeOfCurrentPeriodDEI", FactValueKind.TEXT, "HY"),
        ),
        relationships=(), footnotes=(), validation_issues=(),
    )
    enriched, _ = build("edinet", tmp_path, FakeEdinet({})).fiscal_focus(bundle)
    assert (enriched.filing.fiscal_year, enriched.filing.fiscal_period) == (2025, "H1")


# AC-4: without corp_code OpenDART searches at most 3 months, so longer windows are split.
async def test_ac4_dart_long_window_is_split(tmp_path) -> None:
    fake = FakeDart([[]])
    await build("dart", tmp_path, fake).discover(request(filed_from=date(2024, 1, 15), filed_to=date(2024, 8, 10)))
    windows = [(r.url.params["bgn_de"], r.url.params["end_de"]) for r in fake.requests]
    assert windows == [("20240115", "20240414"), ("20240415", "20240714"), ("20240715", "20240810")]

    scoped = FakeDart([[]])
    await build("dart", tmp_path / "s", scoped).discover(
        request(filed_from=date(2024, 1, 15), filed_to=date(2024, 8, 10), entity="00126380")
    )
    assert [(r.url.params["bgn_de"], r.url.params["end_de"]) for r in scoped.requests] == [("20240115", "20240810")]
