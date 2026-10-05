from datetime import date
from decimal import Decimal
from hashlib import sha256

import pytest

from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    XbrlConcept,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlRelationship,
    XbrlRole,
)
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import role_category, role_sort_code
from sec_xbrl_warehouse.normalization.statements import (
    TOTAL_LABEL_ROLE,
    StatementKind,
    build_statement_lines,
    classify_roles,
)

PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
ROLE = "http://example.com/role/"


@pytest.mark.parametrize(
    ("definition", "code", "category"),
    [
        ("100000 - Statement - Consolidated Balance Sheets", "100000", "statement"),
        ("9952153 - Disclosure - Revenue (Details)", "9952153", "disclosure"),
        ("0000001 - Document - Cover", "0000001", "document"),
        ("Balance Sheet", None, "other"),
        (None, None, "other"),
        ("12 - Schedule - Something", "12", "other"),
    ],
)
def test_role_definition_parsing(definition, code, category) -> None:
    assert role_sort_code(definition) == code
    assert role_category(definition) == category


def _arc(role: str, parent: str, child: str, order: int, preferred: str | None = None):
    return XbrlRelationship(
        relationship_id=sha256(f"{role}{parent}{child}".encode()).hexdigest(),
        relationship_type=RelationshipType.PRESENTATION,
        link_role_uri=role,
        arc_role_uri=PARENT_CHILD,
        from_qname=f"ex:{parent}",
        to_qname=f"ex:{child}",
        order=Decimal(order),
        preferred_label=preferred,
    )


def _bundle(roles: tuple[XbrlRole, ...], relationships, facts=(), concepts=()):
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession="0000000009-26-000009",
            cik=9,
            company_name="Rules Corp",
            form="10-K",
            filed_date=date(2026, 2, 1),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/9/9",
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
        relationships=tuple(relationships),
        footnotes=(),
        validation_issues=(),
        roles=roles,
    )


def _role(name: str, definition: str) -> XbrlRole:
    return XbrlRole(
        role_uri=ROLE + name,
        definition=definition,
        sort_code=role_sort_code(definition),
        category=role_category(definition),
    )


def test_definitions_classify_every_statement_kind_and_skip_others() -> None:
    roles = (
        _role("CI", "300000 - Statement - Consolidated Statements of Comprehensive Income"),
        _role("EQ", "500000 - Statement - Consolidated Statements of Shareholders' Equity"),
        _role("FP", "100000 - Statement - Statement of Financial Position"),
        _role("OPS", "200000 - Statement - Consolidated Statements of Operations"),
        _role("CF", "400000 - Statement - Consolidated Statements of Cash Flows"),
        _role("BSP", "100100 - Statement - Balance Sheets (Parenthetical)"),
        _role("TAX", "900000 - Disclosure - Income Taxes"),
    )
    relationships = [_arc(role.role_uri, "Root", "Item", 1) for role in roles]
    assert classify_roles(_bundle(roles, relationships)) == {
        ROLE + "FP": StatementKind.BALANCE_SHEET,
        ROLE + "OPS": StatementKind.INCOME_STATEMENT,
        ROLE + "CI": StatementKind.COMPREHENSIVE_INCOME,
        ROLE + "CF": StatementKind.CASH_FLOW,
        ROLE + "EQ": StatementKind.EQUITY,
    }


def test_uri_fallback_needs_an_anchor_and_skips_detail_roles() -> None:
    relationships = [
        _arc(ROLE + "IncomeTaxDetails", "Root", "IncomeTaxExpenseBenefit", 1),
        _arc(ROLE + "ConsolidatedStatementsOfIncome", "Root", "Revenues", 1),
        _arc(ROLE + "StatementsOfIncomeWithoutAnchor", "Root", "Other", 1),
    ]
    assert classify_roles(_bundle((), relationships)) == {
        ROLE + "ConsolidatedStatementsOfIncome": StatementKind.INCOME_STATEMENT
    }


def test_lines_use_preferred_labels_and_survive_cycles() -> None:
    role = _role("OPS", "200000 - Statement - Statements of Operations")
    concepts = [
        XbrlConcept(
            namespace="http://example.com",
            name="Costs",
            qname="ex:Costs",
            labels=(
                ConceptLabel(
                    role_uri="http://www.xbrl.org/2003/role/label", language="en-US", text="Costs"
                ),
                ConceptLabel(role_uri=TOTAL_LABEL_ROLE, language="en-US", text="Total costs"),
            ),
        )
    ]
    relationships = [
        _arc(role.role_uri, "Root", "Revenues", 1),
        _arc(role.role_uri, "Root", "Costs", 2, preferred=TOTAL_LABEL_ROLE),
        _arc(role.role_uri, "Costs", "Freight", 1),
        _arc(role.role_uri, "Freight", "Costs", 1),  # cycle below the root
    ]
    lines = build_statement_lines(_bundle((role,), relationships, concepts=concepts))
    assert [(line.line_number, line.depth, line.concept_qname) for line in lines] == [
        (1, 0, "ex:Root"),
        (2, 1, "ex:Revenues"),
        (3, 1, "ex:Costs"),
        (4, 2, "ex:Freight"),
    ]
    costs = lines[2]
    assert costs.label == "Total costs" and costs.is_total
    assert lines[1].label == "Revenues" and not lines[1].is_total


def test_fully_cyclic_tree_still_terminates() -> None:
    role = _role("OPS", "200000 - Statement - Statements of Operations")
    relationships = [
        _arc(role.role_uri, "A", "B", 1),
        _arc(role.role_uri, "B", "A", 1),
    ]
    lines = build_statement_lines(_bundle((role,), relationships))
    assert [(line.depth, line.concept_qname) for line in lines] == [(0, "ex:A"), (1, "ex:B")]


def test_values_are_one_per_period_sorted_latest_first() -> None:
    role = _role("BS", "100000 - Statement - Balance Sheets")

    def fact(fact_id: str, year: int, value: int) -> XbrlFact:
        return XbrlFact(
            fact_id=fact_id,
            concept_namespace="http://example.com",
            concept_name="Assets",
            concept_qname="ex:Assets",
            context_id=f"c{year}",
            context_hash="0" * 64,
            value_kind=FactValueKind.NUMERIC,
            raw_value=str(value),
            value_decimal=Decimal(value),
            period_kind=PeriodKind.INSTANT,
            period_instant=date(year, 12, 31),
            entity_scheme="s",
            entity_identifier="1",
            dimension_hash="0" * 64,
            duplicate_set_hash=fact_id,
            source_document="x.htm",
        )

    facts = [fact("a24", 2024, 5), fact("a25", 2025, 7), fact("a25b", 2025, 8)]
    relationships = [_arc(role.role_uri, "Root", "Assets", 1)]
    (line,) = [
        item
        for item in build_statement_lines(_bundle((role,), relationships, facts=facts))
        if item.concept_qname == "ex:Assets"
    ]
    assert [value.fact_id for value in line.values] == ["a25", "a24"]


def test_bundles_written_before_roles_still_load() -> None:
    bundle = _bundle((), ())
    legacy = bundle.model_dump(mode="json")
    legacy.pop("roles")
    assert XbrlExtractionBundle.model_validate(legacy).roles == ()


def test_combined_operations_and_comprehensive_income_is_an_income_statement() -> None:
    combined = _role(
        "OCI", "200000 - Statement - Statements of Operations and Comprehensive Income"
    )
    ci_only = _role("CI", "300000 - Statement - Statements of Comprehensive Income")
    relationships = [
        _arc(combined.role_uri, "Root", "Revenues", 1),
        _arc(combined.role_uri, "Root", "ComprehensiveIncomeNetOfTax", 2),
        _arc(ci_only.role_uri, "Root", "NetIncomeLoss", 1),
        _arc(ci_only.role_uri, "Root", "ComprehensiveIncomeNetOfTax", 2),
    ]
    assert classify_roles(_bundle((combined, ci_only), relationships)) == {
        combined.role_uri: StatementKind.INCOME_STATEMENT,
        ci_only.role_uri: StatementKind.COMPREHENSIVE_INCOME,
    }


def test_hypercube_lines_are_flagged_and_member_lines_get_dimensional_values() -> None:
    from sec_xbrl_warehouse.domain.xbrl import DimensionMember
    from sec_xbrl_warehouse.normalization.statements import LineRole

    role = _role("OPS", "200000 - Statement - Statements of Operations")
    ns = "http://example.com"

    def concept(name: str, group: str | None = None, datatype: str | None = None) -> XbrlConcept:
        return XbrlConcept(
            namespace=ns,
            name=name,
            qname=f"ex:{name}",
            substitution_group_qname=group,
            datatype_qname=datatype,
            abstract=group is not None and group != "xbrli:item",
        )

    concepts = [
        concept("StatementTable", "xbrldt:hypercubeItem"),
        concept("ProductOrServiceAxis", "xbrldt:dimensionItem"),
        concept("ProductOrServiceDomain", "xbrli:item", "dtr-types:domainItemType"),
        concept("ProductMember", "xbrli:item", "dtr-types:domainItemType"),
        concept("Revenues", "xbrli:item", "xbrli:monetaryItemType"),
    ]
    relationships = [
        _arc(role.role_uri, "Root", "StatementTable", 1),
        _arc(role.role_uri, "StatementTable", "ProductOrServiceAxis", 1),
        _arc(role.role_uri, "ProductOrServiceAxis", "ProductOrServiceDomain", 1),
        _arc(role.role_uri, "ProductOrServiceDomain", "ProductMember", 1),
        _arc(role.role_uri, "StatementTable", "LineItems", 2),
        _arc(role.role_uri, "LineItems", "Revenues", 1),
    ]

    def revenue(fact_id: str, value: int, dimensions=()) -> XbrlFact:
        return XbrlFact(
            fact_id=fact_id,
            concept_namespace=ns,
            concept_name="Revenues",
            concept_qname="ex:Revenues",
            context_id=fact_id,
            context_hash="0" * 64,
            value_kind=FactValueKind.NUMERIC,
            raw_value=str(value),
            value_decimal=Decimal(value),
            period_kind=PeriodKind.DURATION,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            entity_scheme="s",
            entity_identifier="1",
            dimensions=dimensions,
            dimension_hash="1" * 64 if dimensions else "0" * 64,
            duplicate_set_hash=fact_id,
            source_document="x.htm",
        )

    product = DimensionMember(
        axis_namespace=ns,
        axis_name="ProductOrServiceAxis",
        member_namespace=ns,
        member_name="ProductMember",
    )
    other = DimensionMember(
        axis_namespace=ns, axis_name="SegmentAxis", member_namespace=ns, member_name="East"
    )
    facts = [
        revenue("total", 100),
        revenue("products", 60, (product,)),
        revenue("two-dims", 10, (product, other)),
    ]
    lines = build_statement_lines(_bundle((role,), relationships, facts=facts, concepts=concepts))
    roles = {line.concept_qname: line.line_role for line in lines}
    assert roles["ex:StatementTable"] == LineRole.TABLE
    assert roles["ex:ProductOrServiceAxis"] == LineRole.AXIS
    assert roles["ex:ProductOrServiceDomain"] == LineRole.DOMAIN
    assert roles["ex:ProductMember"] == LineRole.MEMBER
    assert roles["ex:Revenues"] == LineRole.CONCEPT
    member = next(line for line in lines if line.concept_qname == "ex:ProductMember")
    assert [(value.fact_id, value.concept_qname) for value in member.values] == [
        ("products", "ex:Revenues")
    ]
    total = next(line for line in lines if line.concept_qname == "ex:Revenues")
    assert [value.fact_id for value in total.values] == ["total"]
