"""Independent verification tests for 10-financial-statement-lines (synthetic bundles)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    DimensionMember,
    XbrlConcept,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlRelationship,
    XbrlUnit,
)

NS = "http://fasb.org/us-gaap/2025"
PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
IS_ROLE = "http://www.example.com/role/CONSOLIDATEDSTATEMENTSOFOPERATIONS"
CF_ROLE = "http://www.example.com/role/CONSOLIDATEDSTATEMENTSOFCASHFLOWS"
SEG_ROLE = "http://www.example.com/role/SegmentInformationDetails"


def _concept(name: str, abstract: bool = False) -> XbrlConcept:
    return XbrlConcept(
        namespace=NS,
        name=name,
        qname=f"us-gaap:{name}",
        period_type="duration",
        abstract=abstract,
        labels=(ConceptLabel(role_uri="http://www.xbrl.org/2003/role/label", language="en-US", text=name),),
    )


def _arc(role: str, parent: str, child: str, order: int) -> XbrlRelationship:
    return XbrlRelationship(
        relationship_id=sha256(f"{role}-{parent}-{child}".encode()).hexdigest(),
        relationship_type=RelationshipType.PRESENTATION,
        link_role_uri=role,
        arc_role_uri=PARENT_CHILD,
        from_qname=f"us-gaap:{parent}",
        to_qname=f"us-gaap:{child}",
        order=Decimal(order),
    )


def _fact(name: str, value: int, fact_id: str, *, ordinal: int = 1, dimensions=()) -> XbrlFact:
    return XbrlFact(
        fact_id=fact_id,
        concept_namespace=NS,
        concept_name=name,
        concept_qname=f"us-gaap:{name}",
        context_id="D2025",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value=str(value),
        value_decimal=Decimal(value),
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimensions=dimensions,
        dimension_hash="1" * 64 if dimensions else "0" * 64,
        duplicate_ordinal=ordinal,
        duplicate_set_hash=f"dup-{name}",
        source_document="x.htm",
    )


def _bundle() -> XbrlExtractionBundle:
    segment = (
        DimensionMember(
            axis_namespace=NS,
            axis_name="StatementBusinessSegmentsAxis",
            member_namespace="http://www.example.com/2025",
            member_name="ServicesMember",
        ),
    )
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession="0000000005-26-000005", cik=5, company_name="Synthetic", form="10-K",
            filed_date=date(2026, 2, 1), archive_directory_url="https://www.sec.gov/Archives/edgar/data/5/5",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="x.htm", document_type="inline", taxonomy_namespaces=(NS,),
            source_documents=("x.htm",), software_name="arelle", software_version="2.44.4",
        ),
        contexts=(),
        concepts=tuple(
            _concept(name, abstract=name.endswith("Abstract"))
            for name in (
                "IncomeStatementAbstract", "Revenues", "NetIncomeLoss", "EarningsPerShareDiluted",
                "CashFlowAbstract", "NetCashProvidedByUsedInOperatingActivities", "SegmentAbstract",
            )
        ),
        facts=(
            _fact("Revenues", 2000, "rev"),
            _fact("Revenues", 2000, "rev-dup", ordinal=2),
            _fact("Revenues", 700, "rev-seg", dimensions=segment),
            _fact("NetIncomeLoss", 300, "ni"),
            _fact("NetCashProvidedByUsedInOperatingActivities", 420, "cfo"),
        ),
        relationships=(
            _arc(IS_ROLE, "IncomeStatementAbstract", "Revenues", 1),
            _arc(IS_ROLE, "IncomeStatementAbstract", "NetIncomeLoss", 2),
            _arc(IS_ROLE, "IncomeStatementAbstract", "EarningsPerShareDiluted", 3),
            _arc(CF_ROLE, "CashFlowAbstract", "NetIncomeLoss", 1),
            _arc(CF_ROLE, "CashFlowAbstract", "NetCashProvidedByUsedInOperatingActivities", 2),
            _arc(SEG_ROLE, "SegmentAbstract", "Revenues", 1),
        ),
        footnotes=(),
        validation_issues=(),
    )


# AC-2 fallback: bundles without role definitions (older extraction JSON) classify by URI + anchors.
def test_ac2_classification_falls_back_to_role_uri_and_anchor_concepts() -> None:
    from sec_xbrl_warehouse.normalization.statements import StatementKind, classify_roles

    bundle = _bundle()
    assert bundle.roles == ()
    assert classify_roles(bundle) == {
        IS_ROLE: StatementKind.INCOME_STATEMENT,
        CF_ROLE: StatementKind.CASH_FLOW,
    }


# AC-3 boundaries: dimensional and duplicate facts never appear as face-statement values.
def test_ac3_face_lines_use_one_non_dimensional_value() -> None:
    from sec_xbrl_warehouse.normalization.statements import StatementKind, build_statement_lines

    lines = build_statement_lines(_bundle())
    revenue = [
        line for line in lines
        if line.statement_kind == StatementKind.INCOME_STATEMENT and line.concept_qname == "us-gaap:Revenues"
    ]
    assert len(revenue) == 1
    assert [value.fact_id for value in revenue[0].values] == ["rev"]


# AC-3: a concept on two statements appears on both; a line with no fact keeps an empty value list.
def test_ac3_shared_concepts_and_empty_lines() -> None:
    from sec_xbrl_warehouse.normalization.statements import StatementKind, build_statement_lines

    lines = build_statement_lines(_bundle())
    net_income = {line.statement_kind for line in lines if line.concept_qname == "us-gaap:NetIncomeLoss"}
    assert net_income == {StatementKind.INCOME_STATEMENT, StatementKind.CASH_FLOW}
    eps = next(line for line in lines if line.concept_qname == "us-gaap:EarningsPerShareDiluted")
    assert eps.values == ()
    assert all(line.role_uri != SEG_ROLE for line in lines)


# AC-4: lines are stored in ClickHouse and read back in presentation order for the latest revision.
@pytest.mark.integration
def test_ac4_statement_lines_round_trip_through_clickhouse(clickhouse_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    warehouse = ClickHouseWarehouse(clickhouse_settings)
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        bundle = _bundle()
        counts = warehouse.insert_xbrl_bundle(bundle)
        warehouse.publish_xbrl_bundle(bundle, counts)
        rows = warehouse.query(
            "SELECT statement_kind, line_number, concept_qname FROM v_statement_lines_latest "
            "WHERE accession = %(accession)s ORDER BY statement_kind, line_number",
            {"accession": bundle.filing.accession},
        )
    finally:
        warehouse.close()
    income = [row["concept_qname"] for row in rows if row["statement_kind"] == "income_statement"]
    assert income == ["us-gaap:IncomeStatementAbstract", "us-gaap:Revenues", "us-gaap:NetIncomeLoss", "us-gaap:EarningsPerShareDiluted"]
