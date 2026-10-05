"""Independent verification tests for 12-disclosure-views."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from bundle12v import CLASS, DEI, EXT, GAAP, PRODUCT, SEGMENTS, SRT, bundle, concept, dim, fact

from sec_xbrl_warehouse.domain.enums import FactValueKind

T = FactValueKind.TEXT


# AC-1: share classes are summed; an absent dei fact leaves its field empty.
def test_ac1_share_classes_sum_and_missing_fields_are_none() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import cover_page

    page = cover_page(
        bundle(
            [
                fact(DEI, "DocumentType", "10-K", kind=T),
                fact(DEI, "EntityCommonStockSharesOutstanding", 5_800_000_000, instant=date(2026, 1, 20),
                     unit="xbrli:shares", dims=(dim(*CLASS, GAAP, "CommonClassAMember"),)),
                fact(DEI, "EntityCommonStockSharesOutstanding", 5_500_000_000, instant=date(2026, 1, 20),
                     unit="xbrli:shares", dims=(dim(*CLASS, GAAP, "CommonClassCMember"),)),
            ]
        )
    )
    assert page.shares_outstanding == Decimal(11_300_000_000)
    assert page.trading_symbol is None
    assert page.public_float is None
    assert page.amendment is None


# AC-1: co-registrant (LegalEntityAxis) cover facts never override the registrant's own.
def test_ac1_co_registrant_facts_are_ignored() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import cover_page

    page = cover_page(
        bundle(
            [
                fact(DEI, "EntityRegistrantName", "Parent Co", kind=T),
                fact(DEI, "EntityRegistrantName", "Subsidiary LLC", kind=T,
                     dims=(dim(DEI, "LegalEntityAxis", EXT, "SubsidiaryMember"),)),
            ]
        )
    )
    assert page.registrant_name == "Parent Co"


# AC-2: only allowlisted breakdown axes; other axes and consolidated facts are excluded.
def test_ac2_non_breakdown_axes_and_consolidated_totals_are_excluded() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import segment_rows

    rows = segment_rows(
        bundle(
            [
                fact(GAAP, "Revenues", 1000, fid="consolidated"),
                fact(GAAP, "Revenues", 900,
                     dims=(dim(GAAP, "RestatementAxis", SRT, "ScenarioPreviouslyReportedMember"),)),
                fact(GAAP, "Revenues", 300, dims=(dim(*PRODUCT, EXT, "ServicesMember"),)),
            ],
            concepts=[concept(EXT, "ServicesMember", "Services")],
        )
    )
    assert [(row.axis, row.member_label, row.value) for row in rows] == [("product", "Services", Decimal(300))]


# AC-2: a segment x product fact keeps both members; eliminations are flagged.
def test_ac2_multi_axis_rows_and_eliminations() -> None:
    from sec_xbrl_warehouse.normalization.disclosures import segment_rows

    rows = segment_rows(
        bundle(
            [
                fact(GAAP, "Revenues", 120, dims=(
                    dim(*SEGMENTS, EXT, "CloudSegmentMember"), dim(*PRODUCT, EXT, "ServicesMember"))),
                fact(GAAP, "Revenues", -15, dims=(
                    dim(SRT, "ConsolidationItemsAxis", SRT, "IntersegmentEliminationMember"),)),
            ],
            concepts=[concept(EXT, "CloudSegmentMember", "Cloud"), concept(EXT, "ServicesMember", "Services")],
        )
    )
    combined = next(row for row in rows if row.value == Decimal(120))
    assert {(m.axis, m.label) for m in combined.members} == {("segment", "Cloud"), ("product", "Services")}
    elimination = next(row for row in rows if row.value == Decimal(-15))
    assert elimination.is_elimination


# AC-3/AC-4: cover pages, segment rows and text blocks are queryable from ClickHouse.
@pytest.mark.integration
def test_ac4_views_return_latest_published_rows(clickhouse_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    data = bundle(
        [
            fact(DEI, "DocumentType", "10-K", kind=T),
            fact(GAAP, "Revenues", 300, dims=(dim(*PRODUCT, EXT, "ServicesMember"),)),
            fact(GAAP, "RevenueRecognitionPolicyTextBlock", "<p>Revenue is recognized when...</p>", kind=T),
            fact(GAAP, "SegmentReportingDisclosureTextBlock", "<p>The Company has five segments.</p>", kind=T),
        ],
        concepts=[concept(EXT, "ServicesMember", "Services")],
    )
    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        counts = warehouse.insert_xbrl_bundle(data)
        warehouse.publish_xbrl_bundle(data, counts)
        cover = warehouse.query("SELECT document_type FROM v_cover_pages_latest WHERE accession = %(a)s", {"a": data.filing.accession})
        segments = warehouse.query("SELECT member_label, value FROM v_segment_facts_latest WHERE accession = %(a)s", {"a": data.filing.accession})
        texts = warehouse.query(
            "SELECT concept_name, is_policy FROM v_text_blocks_latest WHERE accession = %(a)s ORDER BY concept_name",
            {"a": data.filing.accession},
        )
    finally:
        warehouse.close()
    assert [row["document_type"] for row in cover] == ["10-K"]
    assert [(row["member_label"], Decimal(str(row["value"]))) for row in segments] == [("Services", Decimal(300))]
    assert [(row["concept_name"], bool(row["is_policy"])) for row in texts] == [
        ("RevenueRecognitionPolicyTextBlock", True),
        ("SegmentReportingDisclosureTextBlock", False),
    ]
