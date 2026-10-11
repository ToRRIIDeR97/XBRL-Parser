from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

pa = pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    DimensionMember,
    XbrlConcept,
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlFootnote,
    XbrlRelationship,
    XbrlUnit,
)
from sec_xbrl_warehouse.exceptions import WarehouseWriteError
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import (
    canonical_fact_schema,
    clickhouse_facts_schema,
    xbrl_warehouse_schema,
)
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    concepts_table,
    contexts_table,
    facts_table,
    footnotes_table,
    relationships_table,
    silver_concepts_table,
    silver_facts_table,
    validate_canonical_facts,
)


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )


def _bundle() -> XbrlExtractionBundle:
    filing = _filing()
    context = XbrlContext(
        context_id="c1",
        context_hash="h1",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dimensions=(
            DimensionMember(
                axis_namespace="http://xbrl.sec.gov/dei/2025",
                axis_name="EntityCommonStockSharesOutstandingAxis",
                member_namespace="http://xbrl.sec.gov/dei/2025",
                member_name="SharesOutstandingMember",
            ),
        ),
        source_document="example-20251231.htm",
    )
    concept = XbrlConcept(
        namespace="http://fasb.org/us-gaap/2025",
        name="Revenues",
        qname="us-gaap:Revenues",
        taxonomy_version="2025",
        datatype_qname="xbrli:monetaryItemType",
        period_type="duration",
        balance="credit",
        labels=(
            ConceptLabel(
                role_uri="http://www.xbrl.org/2003/role/label", language="en-US", text="Revenues"
            ),
        ),
        documentation="Total revenue.",
    )
    fact = XbrlFact(
        fact_id="f1",
        concept_namespace="http://fasb.org/us-gaap/2025",
        concept_name="Revenues",
        concept_qname="us-gaap:Revenues",
        taxonomy_version="2025",
        context_id="c1",
        context_hash="h1",
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value="123456789",
        value_decimal=Decimal("123456789"),
        is_nil=False,
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash="dh1",
        duplicate_set_hash="dup1",
        source_document="example-20251231.htm",
        source_fact_id="f1",
    )
    relationship = XbrlRelationship(
        relationship_id="r1",
        relationship_type=RelationshipType.CALCULATION,
        link_role_uri="http://www.example.com/role/Statement",
        arc_role_uri="http://www.xbrl.org/2003/arcrole/summation-item",
        from_qname="us-gaap:Revenues",
        to_qname="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        order=Decimal("1.0"),
        weight=Decimal("1.0"),
        source_document="example-20251231_htm.xml",
    )
    footnote = XbrlFootnote(
        footnote_id="n1",
        role_uri="http://www.example.com/role/footnote",
        language="en-US",
        text="Note text.",
        linked_fact_ids=("f1",),
    )
    return XbrlExtractionBundle(
        filing=filing,
        metadata=XbrlDocumentMetadata(
            entrypoint="example-20251231.htm",
            document_type="inline",
            taxonomy_namespaces=("http://fasb.org/us-gaap/2025",),
            source_documents=("example-20251231.htm",),
            software_name="arelle",
            software_version="2.44.4",
        ),
        contexts=(context,),
        concepts=(concept,),
        facts=(fact,),
        relationships=(relationship,),
        footnotes=(footnote,),
        validation_issues=(),
    )


def test_contexts_table_matches_arrow_contract() -> None:
    table = contexts_table(_bundle())
    assert table.schema == xbrl_warehouse_schema("contexts")
    assert table.num_rows == 1


def test_concepts_table_matches_arrow_contract() -> None:
    # Silver keeps every concept; ClickHouse `xbrl_concepts` keeps extensions only.
    table = silver_concepts_table(_bundle())
    assert table.schema == xbrl_warehouse_schema("concepts")
    assert table.num_rows == 1
    extensions = concepts_table(_bundle())
    assert extensions.schema == xbrl_warehouse_schema("concepts")
    assert extensions.num_rows == sum(concept.is_extension for concept in _bundle().concepts)


def test_facts_table_matches_arrow_contract() -> None:
    table = silver_facts_table(_bundle())
    assert table.schema == xbrl_warehouse_schema("facts")
    assert facts_table(_bundle()).schema == clickhouse_facts_schema()
    assert table.num_rows == 1
    assert table.column("value_decimal")[0].as_py() == Decimal("123456789")
    assert table.column("unit_numerator_json")[0].as_py() == '["iso4217:USD"]'


def test_relationships_table_matches_arrow_contract() -> None:
    table = relationships_table(_bundle())
    assert table.schema == xbrl_warehouse_schema("relationships")
    assert table.num_rows == 1


def test_footnotes_table_matches_arrow_contract() -> None:
    table = footnotes_table(_bundle())
    assert table.schema == xbrl_warehouse_schema("footnotes")
    assert table.num_rows == 1
    assert table.column("linked_fact_ids")[0].as_py() == ["f1"]


def test_canonical_fact_validation_requires_mapping() -> None:
    real_fact = CanonicalFact(
        canonical_fact_id="cf1",
        raw_fact_id="f1",
        accession="0000000001-26-000001",
        cik=1,
        filed_date=date(2026, 2, 15),
        form="10-K",
        canonical_metric="Revenue",
        normalized_value_decimal=Decimal("1"),
        sign_multiplier=1,
        period_kind="duration",
        dimensions_json="[]",
        mapping_id=UUID("11111111-1111-1111-1111-111111111111"),
        mapping_version=1,
        mapping_confidence=1.0,
        mapping_explanation="approved",
        extraction_revision=1,
    )
    bad = real_fact.model_copy(update={"mapping_id": UUID(int=0)})
    with pytest.raises(WarehouseWriteError, match="mapping lineage"):
        validate_canonical_facts([bad])
    assert validate_canonical_facts([real_fact]) is None


def _canonical_mapping_id() -> UUID:
    return UUID("11111111-1111-1111-1111-111111111111")


def test_canonical_fact_conversion_produces_valid_arrow() -> None:
    fact = CanonicalFact(
        canonical_fact_id="cf1",
        raw_fact_id="f1",
        accession="0000000001-26-000001",
        cik=1,
        filed_date=date(2026, 2, 15),
        form="10-K",
        canonical_metric="Revenue",
        normalized_value_decimal=Decimal("123456789"),
        normalized_unit="USD",
        sign_multiplier=1,
        period_kind="duration",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dimensions_json="[]",
        mapping_id=UUID("11111111-1111-1111-1111-111111111111"),
        mapping_version=1,
        mapping_confidence=1.0,
        mapping_explanation="approved",
        extraction_revision=1,
    )
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import _canonical_fact_row

    row = _canonical_fact_row(fact)
    table = pa.Table.from_pylist([row], schema=canonical_fact_schema())
    assert table.num_rows == 1
    assert table.column("canonical_metric")[0].as_py() == "Revenue"


def test_hash_columns_encode_to_32_bytes() -> None:
    import hashlib

    import pyarrow as pa

    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import encode_hash_columns

    digest = "ab" * 32
    table = pa.table(
        {
            "fact_id": [digest],
            "context_hash": [""],
            "footnote_ids": [[digest]],
            "concept_name": ["Revenues"],
        }
    )
    encoded = encode_hash_columns("xbrl_facts", table)
    assert encoded.column("fact_id")[0].as_py() == bytes.fromhex(digest)
    assert encoded.column("context_hash")[0].as_py() == bytes(32)
    assert encoded.column("footnote_ids")[0].as_py() == [bytes.fromhex(digest)]
    assert encoded.column("concept_name")[0].as_py() == "Revenues"
    assert encode_hash_columns("sec_tags", table).equals(table)
    # A non-hex identifier is stored as its SHA-256 (matching the upgrade's mapping).
    other = encode_hash_columns("xbrl_facts", table.set_column(0, "fact_id", pa.array(["f1"])))
    assert other.column("fact_id")[0].as_py() == hashlib.sha256(b"f1").digest()


@pytest.mark.parametrize(
    ("kind", "raw", "decimal", "text", "nil", "scale"),
    [
        ("numeric", "1000", "1000", None, False, 0),
        ("numeric", "1.50", "1.50", None, False, 2),
        ("numeric", "-5", "-5", None, False, 0),
        ("numeric", "1,000", "1000", None, False, None),
        ("numeric", "1E+3", "1E+3", None, False, None),
        ("numeric", "1000", None, None, False, None),
        ("numeric", "", None, None, True, None),
        ("text", "Plain", None, "Plain", False, 0),
        ("text", "<p>Plain</p>", None, "Plain", False, None),
        ("boolean", "true", None, None, False, None),
    ],
)
def test_raw_value_scale_rule(kind, raw, decimal, text, nil, scale) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import raw_value_scale

    fact = (
        _bundle()
        .facts[0]
        .model_copy(
            update={
                "value_kind": FactValueKind(kind),
                "raw_value": raw,
                "value_decimal": Decimal(decimal) if decimal is not None else None,
                "value_text": text,
                "is_nil": nil,
            }
        )
    )
    assert raw_value_scale(fact) == scale


def test_bundle_insert_skips_taxonomy_rows_already_inserted_by_this_process() -> None:
    from types import SimpleNamespace

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    inserted: list[tuple[str, int]] = []
    client = SimpleNamespace(
        insert_arrow=lambda name, table: inserted.append((name.split(".")[-1], table.num_rows))
    )
    warehouse = ClickHouseWarehouse(Settings(env="test"), client=client)
    base = _bundle()
    standard = base.concepts[0]
    extension = standard.model_copy(
        update={
            "namespace": "http://example.com/ext",
            "qname": "ext:Custom",
            "name": "Custom",
            "is_extension": True,
        }
    )
    bundle = base.model_copy(update={"concepts": (standard, extension)})

    first = warehouse.insert_xbrl_bundle(bundle)
    assert first["concepts"] == 1  # extension concepts only
    assert ("taxonomy_concepts", 1) in inserted
    inserted.clear()
    second = warehouse.insert_xbrl_bundle(bundle)
    assert second == first
    assert "taxonomy_concepts" not in {name for name, _ in inserted}
