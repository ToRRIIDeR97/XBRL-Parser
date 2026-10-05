from datetime import date
from decimal import Decimal
from hashlib import sha256

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    DimensionMember,
    XbrlConcept,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlUnit,
)
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    cover_pages_table,
    segment_facts_table,
)
from sec_xbrl_warehouse.normalization.disclosures import cover_page, segment_rows

GAAP = "http://fasb.org/us-gaap/2025"
SRT = "http://fasb.org/srt/2025"
DEI = "http://xbrl.sec.gov/dei/2025"
EXT = "http://www.example.com/20250927"
D = Decimal


def _dim(axis_ns, axis, member_ns, member):
    return DimensionMember(
        axis_namespace=axis_ns, axis_name=axis, member_namespace=member_ns, member_name=member
    )


def _fact(ns, name, value, *, dims=(), instant=None, text=False, ordinal=1, fid=None):
    numeric = not text
    return XbrlFact(
        fact_id=sha256((fid or f"{name}{value}{dims}{ordinal}").encode()).hexdigest(),
        concept_namespace=ns,
        concept_name=name,
        concept_qname=f"x:{name}",
        context_id="c",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="u", numerator_measures=("iso4217:USD",)) if numeric else None,
        value_kind=FactValueKind.NUMERIC if numeric else FactValueKind.TEXT,
        raw_value=str(value),
        value_decimal=D(value) if numeric else None,
        value_text=None if numeric else str(value),
        period_kind=PeriodKind.INSTANT if instant else PeriodKind.DURATION,
        period_start=None if instant else date(2024, 9, 29),
        period_end=None if instant else date(2025, 9, 27),
        period_instant=instant,
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimensions=dims,
        dimension_hash="0" * 64,
        duplicate_ordinal=ordinal,
        duplicate_set_hash="0" * 64,
        source_document="x.htm",
    )


def _bundle(facts, concepts=()):
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession="0000000001-25-000001",
            cik=1,
            company_name="Example",
            form="10-K",
            filed_date=date(2025, 10, 31),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="x.htm",
            document_type="inline",
            taxonomy_namespaces=(),
            source_documents=("x.htm",),
            software_name="arelle",
            software_version="test",
        ),
        contexts=(),
        concepts=tuple(concepts),
        facts=tuple(facts),
        relationships=(),
        footnotes=(),
        validation_issues=(),
    )


CLASS = (GAAP, "StatementClassOfStockAxis")


def test_cover_page_prefers_plain_shares_and_reads_raw_text_values() -> None:
    page = cover_page(
        _bundle(
            [
                _fact(DEI, "EntityCommonStockSharesOutstanding", 7, instant=date(2026, 1, 1)),
                _fact(
                    DEI,
                    "EntityCommonStockSharesOutstanding",
                    3,
                    instant=date(2026, 1, 1),
                    dims=(_dim(*CLASS, GAAP, "CommonClassAMember"),),
                ),
                _fact(DEI, "DocumentPeriodEndDate", "2025-09-27", text=True),
                _fact(DEI, "AmendmentFlag", "true", text=True),
                # A look-alike namespace is not dei.
                _fact("http://example.com/dei/2025", "TradingSymbol", "NOPE", text=True),
            ]
        )
    )
    assert (page.shares_outstanding, page.shares_outstanding_date) == (D(7), date(2026, 1, 1))
    assert page.period_end_date == date(2025, 9, 27)
    assert page.amendment is True
    assert page.trading_symbol is None


def test_class_sums_use_the_latest_instant_and_skip_co_registrant_classes() -> None:
    legal = _dim(DEI, "LegalEntityAxis", EXT, "SubsidiaryMember")
    page = cover_page(
        _bundle(
            [
                _fact(
                    DEI,
                    "EntityCommonStockSharesOutstanding",
                    100,
                    instant=date(2025, 1, 1),
                    dims=(_dim(*CLASS, GAAP, "CommonClassAMember"),),
                ),
                _fact(
                    DEI,
                    "EntityCommonStockSharesOutstanding",
                    5,
                    instant=date(2026, 1, 1),
                    dims=(_dim(*CLASS, GAAP, "CommonClassAMember"),),
                ),
                _fact(
                    DEI,
                    "EntityCommonStockSharesOutstanding",
                    6,
                    instant=date(2026, 1, 1),
                    dims=(_dim(*CLASS, GAAP, "CommonClassBMember"),),
                ),
                _fact(
                    DEI,
                    "EntityCommonStockSharesOutstanding",
                    1000,
                    instant=date(2026, 1, 1),
                    dims=(_dim(*CLASS, GAAP, "CommonClassAMember"), legal),
                ),
            ]
        )
    )
    assert (page.shares_outstanding, page.shares_outstanding_date) == (D(11), date(2026, 1, 1))


def test_segment_rows_label_fallback_order_and_exclusions() -> None:
    segment = _dim(GAAP, "StatementBusinessSegmentsAxis", EXT, "CloudSegmentMember")
    geography = _dim(
        SRT, "StatementGeographicalAxis", "http://xbrl.sec.gov/country/2025", "USMember"
    )
    rows = segment_rows(
        _bundle(
            [
                # Geography listed first still yields the segment as the primary member.
                _fact(GAAP, "Revenues", 40, dims=(geography, segment)),
                _fact(GAAP, "Revenues", 40, dims=(geography, segment), ordinal=2),
                _fact(
                    GAAP,
                    "Revenues",
                    9,
                    dims=(
                        _dim(SRT, "ConsolidationItemsAxis", SRT, "ConsolidationEliminationsMember"),
                    ),
                ),
                _fact(GAAP, "SegmentPolicyTextBlock", "x", text=True, dims=(segment,)),
                _fact(
                    GAAP,
                    "Revenues",
                    1,
                    dims=(
                        segment,
                        _dim(GAAP, "RestatementAxis", SRT, "ScenarioPreviouslyReportedMember"),
                    ),
                ),
                # A look-alike axis namespace is not allowlisted.
                _fact(
                    GAAP,
                    "Revenues",
                    2,
                    dims=(
                        _dim("http://example.com/srt/2025", "ProductOrServiceAxis", EXT, "XMember"),
                    ),
                ),
            ],
            concepts=[
                XbrlConcept(
                    namespace=EXT,
                    name="CloudSegmentMember",
                    qname="ex:CloudSegmentMember",
                    labels=(
                        ConceptLabel(
                            role_uri="http://www.xbrl.org/2003/role/terseLabel",
                            language="en-US",
                            text="Terse",
                        ),
                        ConceptLabel(
                            role_uri="http://www.xbrl.org/2003/role/label",
                            language="en-US",
                            text="Cloud",
                        ),
                    ),
                )
            ],
        )
    )
    assert [(row.axis, row.member_label, row.value, row.is_elimination) for row in rows] == [
        ("segment", "Cloud", D(40), False),
        ("consolidation", "ConsolidationEliminations", D(9), True),
    ]
    assert [(m.axis, m.qname, m.label) for m in rows[0].members] == [
        ("segment", "ex:CloudSegmentMember", "Cloud"),
        ("geography", "{http://xbrl.sec.gov/country/2025}USMember", "US"),
    ]
    assert rows[0].unit == "USD"


def test_disclosure_tables_match_their_arrow_contracts() -> None:
    bundle = _bundle(
        [
            _fact(DEI, "DocumentType", "10-K", text=True),
            _fact(
                GAAP,
                "Revenues",
                5,
                dims=(_dim(SRT, "ProductOrServiceAxis", EXT, "ServicesMember"),),
            ),
        ]
    )
    covers = cover_pages_table(bundle).to_pylist()
    assert [(row["document_type"], row["amendment"]) for row in covers] == [("10-K", None)]
    (segment,) = segment_facts_table(bundle).to_pylist()
    assert (segment["member_axes"], segment["member_labels"], segment["value"]) == (
        ["product"],
        ["Services"],
        D(5),
    )


def _security(name, value, member):
    return _fact(DEI, name, value, text=True, dims=(_dim(*CLASS, GAAP, member),))


def test_multi_security_cover_takes_ticker_and_exchange_from_common_stock() -> None:
    # AAPL-shaped: listed notes come first in document order, then common stock.
    page = cover_page(
        _bundle(
            [
                _security("Security12bTitle", "0.000% Notes due 2025", "A0.000NotesDue2025Member"),
                _security("TradingSymbol", "AAPL", "A0.000NotesDue2025Member"),
                _security("SecurityExchangeName", "NYSE", "A0.000NotesDue2025Member"),
                _security(
                    "Security12bTitle",
                    "Common Stock, $0.00001 par value per share",
                    "CommonStockMember",
                ),
                _security("TradingSymbol", "AAPL", "CommonStockMember"),
                _security("SecurityExchangeName", "NASDAQ", "CommonStockMember"),
            ]
        )
    )
    assert (page.trading_symbol, page.exchange) == ("AAPL", "NASDAQ")
    assert [(s.member, s.trading_symbol, s.exchange) for s in page.securities] == [
        ("us-gaap:A0.000NotesDue2025Member", "AAPL", "NYSE"),
        ("us-gaap:CommonStockMember", "AAPL", "NASDAQ"),
    ]
    assert page.securities[1].title == "Common Stock, $0.00001 par value per share"


def test_cover_security_fallbacks() -> None:
    # Common stock found by its 12(b) title; else the first listed security.
    titled = cover_page(
        _bundle(
            [
                _security("TradingSymbol", "XPRA", "SeriesAPreferredMember"),
                _security("SecurityExchangeName", "NYSE", "SeriesAPreferredMember"),
                _security("Security12bTitle", "Class A Common Stock", "CommonClassAMember"),
                _security("TradingSymbol", "XA", "CommonClassAMember"),
                _security("SecurityExchangeName", "NASDAQ", "CommonClassAMember"),
            ]
        )
    )
    assert (titled.trading_symbol, titled.exchange) == ("XA", "NASDAQ")
    first = cover_page(
        _bundle(
            [
                _security("TradingSymbol", "XPRA", "SeriesAPreferredMember"),
                _security("SecurityExchangeName", "NYSE", "SeriesAPreferredMember"),
                _security("TradingSymbol", "XPRB", "SeriesBPreferredMember"),
            ]
        )
    )
    assert (first.trading_symbol, first.exchange) == ("XPRA", "NYSE")
    # A non-dimensional ticker wins; the exchange comes from the matching security.
    plain = cover_page(
        _bundle(
            [
                _fact(DEI, "TradingSymbol", "XPRB", text=True),
                _security("TradingSymbol", "XPRA", "SeriesAPreferredMember"),
                _security("SecurityExchangeName", "NYSE", "SeriesAPreferredMember"),
                _security("TradingSymbol", "XPRB", "SeriesBPreferredMember"),
                _security("SecurityExchangeName", "NASDAQ", "SeriesBPreferredMember"),
            ]
        )
    )
    assert (plain.trading_symbol, plain.exchange) == ("XPRB", "NASDAQ")
