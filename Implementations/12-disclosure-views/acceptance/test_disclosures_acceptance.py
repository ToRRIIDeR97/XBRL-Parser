"""Worker-visible acceptance tests for 12-disclosure-views."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from bundle12 import DEI, EXT, GAAP, GEOGRAPHY, SEGMENTS, bundle, concept, dim, fact

from sec_xbrl_warehouse.domain.enums import FactValueKind

T = FactValueKind.TEXT


# AC-1
def test_ac1_cover_page_from_dei_facts() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import cover_page

    page = cover_page(
        bundle(
            [
                fact(DEI, "DocumentType", "10-K", kind=T),
                fact(DEI, "DocumentPeriodEndDate", date(2025, 9, 27), kind=FactValueKind.DATE),
                fact(DEI, "EntityRegistrantName", "Apple Inc.", kind=T),
                fact(DEI, "EntityFilerCategory", "Large Accelerated Filer", kind=T),
                fact(DEI, "AmendmentFlag", False, kind=FactValueKind.BOOLEAN),
                fact(DEI, "TradingSymbol", "AAPL", kind=T),
                fact(DEI, "SecurityExchangeName", "NASDAQ", kind=T),
                fact(DEI, "AuditorName", "Ernst & Young LLP", kind=T),
                fact(DEI, "EntityCommonStockSharesOutstanding", 14776353000, instant=date(2025, 10, 17), unit="xbrli:shares"),
                fact(DEI, "EntityPublicFloat", 3_247_000_000_000, instant=date(2025, 3, 28)),
            ]
        )
    )
    assert page.document_type == "10-K"
    assert page.period_end_date == date(2025, 9, 27)
    assert page.registrant_name == "Apple Inc."
    assert page.filer_category == "Large Accelerated Filer"
    assert page.amendment is False
    assert (page.trading_symbol, page.exchange) == ("AAPL", "NASDAQ")
    assert page.auditor_name == "Ernst & Young LLP"
    assert page.shares_outstanding == Decimal(14776353000)
    assert page.shares_outstanding_date == date(2025, 10, 17)
    assert page.public_float == Decimal(3_247_000_000_000)


# AC-2
def test_ac2_segment_and_geographic_rows_with_member_labels() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import segment_rows

    rows = segment_rows(
        bundle(
            [
                fact(GAAP, "RevenueFromContractWithCustomerExcludingAssessedTax", 416_000, fid="total"),
                fact(GAAP, "RevenueFromContractWithCustomerExcludingAssessedTax", 178_000,
                     dims=(dim(*SEGMENTS, EXT, "AmericasSegmentMember"),)),
                fact(GAAP, "OperatingIncomeLoss", 70_000, dims=(dim(*SEGMENTS, EXT, "AmericasSegmentMember"),)),
                fact(GAAP, "RevenueFromContractWithCustomerExcludingAssessedTax", 151_000,
                     dims=(dim(*GEOGRAPHY, "http://xbrl.sec.gov/country/2025", "USMember"),)),
            ],
            # USMember has no label in the bundle: the label falls back to the member name.
            concepts=[concept(EXT, "AmericasSegmentMember", "Americas")],
        )
    )
    keyed = {(row.concept_qname, row.axis, row.member_label): row.value for row in rows}
    assert keyed == {
        ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "segment", "Americas"): Decimal(178_000),
        ("us-gaap:OperatingIncomeLoss", "segment", "Americas"): Decimal(70_000),
        ("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "geography", "US"): Decimal(151_000),
    }
