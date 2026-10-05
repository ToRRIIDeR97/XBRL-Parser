"""Upgrade a database created with the pre-slimming (main @ 8f16ca0) DDL.

The fixture DDL in tests/fixtures/clickhouse_ddl_main is a verbatim copy of main's
sql/clickhouse. Rows are written in the old encoding (hex strings, full concepts,
every raw value), then `upgrade_ddl` rebuilds the tables and the reader queries must
return the same results.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pyarrow as pa
import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import (
    ConceptLabel,
    XbrlConcept,
    XbrlContext,
    XbrlDocumentMetadata,
    XbrlExtractionBundle,
    XbrlFact,
    XbrlFootnote,
    XbrlRelationship,
    XbrlUnit,
)
from sec_xbrl_warehouse.infrastructure.storage.arrow_schemas import canonical_fact_schema
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
    ClickHouseWarehouse,
    _canonical_fact_row,
    contexts_table,
    footnotes_table,
    relationships_table,
    silver_concepts_table,
    silver_facts_table,
)
from sec_xbrl_warehouse.query.service import FactQuery, QueryService

pytestmark = pytest.mark.integration

OLD_DDL = Path(__file__).parents[1] / "fixtures/clickhouse_ddl_main"
GAAP = "http://fasb.org/us-gaap/2025"
EXT = "http://www.example.com/20251231"
ACCESSION = "0000000001-26-000001"


def _h(text: str) -> str:
    return sha256(text.encode()).hexdigest()


def _fact(name: str, raw: str | None, **values: Any) -> XbrlFact:
    kind = values.pop("kind", FactValueKind.NUMERIC)
    numeric = kind is FactValueKind.NUMERIC
    return XbrlFact(
        fact_id=_h(f"fact-{name}-{raw}"),
        concept_namespace=values.pop("namespace", GAAP),
        concept_name=name,
        concept_qname=values.pop("qname", f"us-gaap:{name}"),
        context_id="c1",
        context_hash=values.pop("context_hash", _h("ctx")),
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)) if numeric else None,
        value_kind=kind,
        raw_value=raw,
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000001",
        dimension_hash=_h("nodims"),
        duplicate_set_hash=_h(f"dup-{name}"),
        source_document="x.htm",
        **values,
    )


def _bundle() -> XbrlExtractionBundle:
    note = _h("footnote")
    facts = (
        _fact("Revenues", "1000", value_decimal=Decimal(1000), footnote_ids=(note,)),
        _fact("CostOfRevenue", "1,234", value_decimal=Decimal(1234)),
        _fact("EarningsPerShareBasic", "1.50", value_decimal=Decimal("1.50")),
        _fact("Description", "Plain text", kind=FactValueKind.TEXT, value_text="Plain text"),
        _fact(
            "RevenueRecognitionPolicyTextBlock",
            "<p>Policy</p>",
            kind=FactValueKind.TEXT,
            value_text="Policy",
        ),
        _fact("Liabilities", None, is_nil=True, kind=FactValueKind.NIL),
        _fact(
            "ServicesNetSales",
            "5",
            value_decimal=Decimal(5),
            namespace=EXT,
            qname="ex:ServicesNetSales",
            context_hash="",
        ),
    )
    concepts = tuple(
        XbrlConcept(
            namespace=namespace,
            name=name,
            qname=f"{prefix}:{name}",
            taxonomy_version="2025" if namespace == GAAP else None,
            is_extension=namespace == EXT,
            labels=(
                ConceptLabel(
                    role_uri="http://www.xbrl.org/2003/role/label", language="en-US", text=name
                ),
            ),
        )
        for namespace, prefix, name in (
            (GAAP, "us-gaap", "Revenues"),
            (GAAP, "us-gaap", "Assets"),
            (EXT, "ex", "ServicesNetSales"),
        )
    )
    return XbrlExtractionBundle(
        filing=FilingRef(
            accession=ACCESSION,
            cik=1,
            company_name="Example Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
        ),
        metadata=XbrlDocumentMetadata(
            entrypoint="x.htm",
            document_type="inline",
            taxonomy_namespaces=(GAAP,),
            source_documents=("x.htm",),
            software_name="arelle",
            software_version="test",
            extracted_at=datetime(2026, 2, 16, tzinfo=UTC),
        ),
        contexts=(
            XbrlContext(
                context_id="c1",
                context_hash=_h("ctx"),
                entity_scheme="http://www.sec.gov/CIK",
                entity_identifier="0000000001",
                period_kind=PeriodKind.DURATION,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                source_document="x.htm",
            ),
        ),
        concepts=concepts,
        facts=facts,
        relationships=(
            XbrlRelationship(
                relationship_id=_h("arc"),
                relationship_type=RelationshipType.PRESENTATION,
                link_role_uri="http://example.com/role/IS",
                arc_role_uri="http://www.xbrl.org/2003/arcrole/parent-child",
                from_qname="us-gaap:IncomeStatementAbstract",
                to_qname="us-gaap:Revenues",
            ),
            # The filer labels us-gaap:Revenues in its own label linkbase.
            XbrlRelationship(
                relationship_id=_h("label-arc"),
                relationship_type=RelationshipType.LABEL,
                link_role_uri="http://www.xbrl.org/2003/role/link",
                arc_role_uri="http://www.xbrl.org/2003/arcrole/concept-label",
                from_qname="us-gaap:Revenues",
                to_qname=f"label:{_h('net sales')}",
                source_document="x.htm",
            ),
        ),
        footnotes=(
            XbrlFootnote(footnote_id=note, text="A note", linked_fact_ids=(facts[0].fact_id,)),
        ),
        validation_issues=(),
    )


def _canonical(bundle: XbrlExtractionBundle) -> CanonicalFact:
    revenue = bundle.facts[0]
    return CanonicalFact(
        canonical_fact_id=_h("canonical"),
        raw_fact_id=revenue.fact_id,
        accession=ACCESSION,
        cik=1,
        filed_date=date(2026, 2, 15),
        form="10-K",
        canonical_metric="revenue",
        normalized_value_decimal=Decimal(1000),
        normalized_unit="USD",
        period_kind="duration",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dimensions_json="[]",
        mapping_id=UUID("11111111-1111-1111-1111-111111111111"),
        mapping_version=1,
        mapping_confidence=1,
        mapping_explanation="upgrade test",
        extraction_revision=1,
    )


def _insert_old(warehouse: ClickHouseWarehouse, table_name: str, table: pa.Table) -> None:
    """Insert hex-encoded rows into an old-DDL table (only the columns it has)."""
    columns = set(warehouse._columns(table_name))
    keep = [name for name in table.column_names if name in columns]
    warehouse.client.insert_arrow(f"{warehouse.database}.{table_name}", table.select(keep))


def _normalize(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode().rstrip("\0")
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str):
        return value.rstrip("\0")
    return value


def _rows(rows: list[dict[str, Any]], keys: set[str] | None = None) -> list[dict[str, Any]]:
    return [
        {key: _normalize(value) for key, value in row.items() if keys is None or key in keys}
        for row in rows
    ]


def _snapshot(warehouse: ClickHouseWarehouse, canonical_keys: set[str]) -> dict[str, Any]:
    service = QueryService(warehouse)
    return {
        "facts": _rows(service.raw_facts(FactQuery(accession=ACCESSION, limit=100))),
        "canonical": _rows(service.canonical_series(cik=1, metric="revenue"), canonical_keys),
        "summary": _rows([service.filing_summary(ACCESSION) or {}]),
        "lineage": _rows(
            warehouse.query(
                "SELECT c.canonical_metric AS metric, f.concept_qname AS concept, f.raw_value AS raw "
                "FROM v_canonical_facts_latest AS c "
                "INNER JOIN v_xbrl_facts_latest AS f ON f.fact_id = c.raw_fact_id"
            )
        ),
    }


def test_upgrade_from_main_ddl_keeps_query_results(clickhouse_settings: Settings) -> None:
    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_test_slim_{uuid4().hex[:12]}"
    assert database.startswith("xbrl_test_")
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    try:
        warehouse.apply_ddl(OLD_DDL)
        bundle = _bundle()
        for table_name, table in (
            ("xbrl_contexts", contexts_table(bundle)),
            ("xbrl_concepts", silver_concepts_table(bundle)),
            ("xbrl_facts", silver_facts_table(bundle)),
            ("xbrl_relationships", relationships_table(bundle)),
            ("xbrl_footnotes", footnotes_table(bundle)),
            (
                "canonical_facts",
                pa.Table.from_pylist(
                    [_canonical_fact_row(_canonical(bundle))], schema=canonical_fact_schema()
                ),
            ),
        ):
            _insert_old(warehouse, table_name, table)
        warehouse.client.insert(
            f"{database}.xbrl_extractions",
            [[ACCESSION, 1, "{}", bundle.metadata.model_dump_json(), datetime.now(UTC)]],
            column_names=[
                "accession",
                "extraction_revision",
                "entity_counts_json",
                "metadata_json",
                "completed_at",
            ],
        )
        old_canonical = set(warehouse._columns("canonical_facts"))
        before = _snapshot(warehouse, old_canonical)
        assert before["facts"] and before["canonical"] and before["lineage"]

        backups = warehouse.upgrade_ddl(Path("sql/clickhouse"))

        assert {
            "xbrl_contexts",
            "xbrl_concepts",
            "xbrl_facts",
            "xbrl_relationships",
            "xbrl_footnotes",
            "canonical_facts",
        } <= set(backups)
        assert all("__backup_2" in name for name in backups.values())
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ()
        after = _snapshot(warehouse, old_canonical)
        assert after == before

        types = {
            row["name"]: row["type"]
            for row in warehouse.query(
                "SELECT name, type FROM system.columns WHERE database = %(db)s "
                "AND table = 'xbrl_facts'",
                {"db": database},
            )
        }
        assert types["fact_id"] == "FixedString(32)"
        assert types["footnote_ids"] == "Array(FixedString(32))"
        # Standard concepts moved to taxonomy_concepts; the backup keeps every row.
        assert warehouse.query("SELECT concept_name FROM xbrl_concepts ORDER BY concept_name") == [
            {"concept_name": "ServicesNetSales"}
        ]
        assert warehouse.query(
            "SELECT concept_name FROM taxonomy_concepts FINAL ORDER BY concept_name"
        ) == [{"concept_name": "Assets"}, {"concept_name": "Revenues"}]
        assert warehouse.query(f"SELECT count() AS n FROM {backups['xbrl_concepts']}") == [{"n": 3}]
        assert warehouse.query(
            "SELECT concept_qname FROM v_filing_concepts_latest ORDER BY concept_qname"
        ) == [{"concept_qname": "ex:ServicesNetSales"}, {"concept_qname": "us-gaap:Revenues"}]
        # The filer's labels stay with the filing; only taxonomy-owned labels are shared.
        revenues = next(
            c for c in silver_concepts_table(bundle).to_pylist() if c["concept_name"] == "Revenues"
        )
        assert warehouse.query(
            "SELECT labels_json FROM v_filing_concepts_latest WHERE concept_name = 'Revenues'"
        ) == [{"labels_json": revenues["labels_json"]}]
        assert warehouse.query(
            "SELECT concept_name, labels_json, labels_rank FROM taxonomy_concepts FINAL "
            "ORDER BY concept_name"
        ) == [
            {
                "concept_name": "Assets",
                "labels_json": next(
                    c["labels_json"]
                    for c in silver_concepts_table(bundle).to_pylist()
                    if c["concept_name"] == "Assets"
                ),
                "labels_rank": 1,
            },
            {"concept_name": "Revenues", "labels_json": "[]", "labels_rank": 0},
        ]
        assert warehouse.query("SELECT accession, concept_qname FROM xbrl_concept_labels") == [
            {"accession": ACCESSION, "concept_qname": "us-gaap:Revenues"}
        ]
        assert sorted(
            row["id"]
            for row in warehouse.query(
                "SELECT lower(hex(relationship_id)) AS id FROM xbrl_relationships"
            )
        ) == sorted(relationship.relationship_id for relationship in bundle.relationships)

        # New writes land in the upgraded schema and read back identically.
        revised = bundle.model_copy(update={"extraction_revision": 2})
        warehouse.publish_xbrl_bundle(revised, warehouse.insert_xbrl_bundle(revised))
        rewritten = _rows(
            QueryService(warehouse).raw_facts(FactQuery(accession=ACCESSION, limit=100))
        )
        drop = {"extraction_revision", "_row_hash"}
        assert [
            {key: value for key, value in row.items() if key not in drop} for row in rewritten
        ] == [
            {key: value for key, value in row.items() if key not in drop} for row in after["facts"]
        ]
        stored = warehouse.query(
            "SELECT concept_name, raw_value, raw_value_scale FROM xbrl_facts "
            "WHERE extraction_revision = 2 ORDER BY concept_name"
        )
        assert {row["concept_name"]: row["raw_value"] for row in stored} == {
            "CostOfRevenue": "1,234",
            "Description": None,
            "EarningsPerShareBasic": None,
            "Liabilities": None,
            "RevenueRecognitionPolicyTextBlock": "<p>Policy</p>",
            "Revenues": None,
            "ServicesNetSales": None,
        }
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()


def test_upgrade_of_hex_statement_and_disclosure_tables(clickhouse_settings: Settings) -> None:
    """Plan 10-12 tables written with hex hashes (FixedString(64)/String) upgrade intact."""
    from sec_xbrl_warehouse.domain.xbrl import DimensionMember, XbrlRole
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import (
        cover_pages_table,
        roles_table,
        segment_facts_table,
        statement_lines_table,
    )

    base = _bundle()
    segment = _fact(
        "Revenues",
        "700",
        value_decimal=Decimal(700),
        dimensions=(
            DimensionMember(
                axis_namespace=GAAP,
                axis_name="StatementBusinessSegmentsAxis",
                member_namespace=EXT,
                member_name="CloudSegmentMember",
            ),
        ),
    )
    document_type = _fact(
        "DocumentType",
        "10-K",
        kind=FactValueKind.TEXT,
        value_text="10-K",
        namespace="http://xbrl.sec.gov/dei/2025",
        qname="dei:DocumentType",
    )
    bundle = base.model_copy(
        update={
            "facts": (*base.facts, segment, document_type),
            "roles": (
                XbrlRole(
                    role_uri="http://example.com/role/IS",
                    definition="100000 - Statement - Income Statement",
                    sort_code="100000",
                    category="statement",
                ),
            ),
        }
    )
    tables = {
        "xbrl_roles": roles_table(bundle),
        "xbrl_statement_lines": statement_lines_table(bundle),
        "xbrl_cover_pages": cover_pages_table(bundle),
        "xbrl_segment_facts": segment_facts_table(bundle),
    }
    assert all(table.num_rows for table in tables.values())

    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_test_slim_{uuid4().hex[:12]}"
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    old_ddl = Path(admin.settings.temp_dir) / f"ddl-{database}"
    try:
        # The plan 10-12 DDL before slimming: hex hashes.
        old_ddl.mkdir(parents=True)
        for path in sorted(Path("sql/clickhouse").glob("[0-9][0-9][0-9]_*.sql")):
            text = path.read_text(encoding="utf-8")
            if path.name in {"025_statement_tables.sql", "027_disclosure_tables.sql"}:
                text = (
                    text.replace("FixedString(32)", "FixedString(64)")
                    .replace(
                        "value_fact_ids Array(FixedString(64))", "value_fact_ids Array(String)"
                    )
                    .replace("    fact_id FixedString(64),", "    fact_id String,")
                )
            (old_ddl / path.name).write_text(text, encoding="utf-8")
        warehouse.apply_ddl(old_ddl)
        for table_name, table in tables.items():
            _insert_old(warehouse, table_name, table)
        before = {
            name: _rows(warehouse.query(f"SELECT * FROM {name} FINAL ORDER BY _row_hash"))
            for name in tables
        }

        backups = warehouse.upgrade_ddl(Path("sql/clickhouse"))

        assert set(tables) <= set(backups)
        assert warehouse.schema_drift(Path("sql/clickhouse")) == ()
        warehouse.client.command(
            f"INSERT INTO {database}.xbrl_extractions "
            "(accession, extraction_revision, entity_counts_json, metadata_json, completed_at) "
            f"VALUES ('{ACCESSION}', 1, '{{}}', '{{}}', now64(3))"
        )
        after = {
            name: _rows(warehouse.query(f"SELECT * FROM {view} ORDER BY _row_hash"))
            for name, view in (
                ("xbrl_statement_lines", "v_statement_lines_latest"),
                ("xbrl_cover_pages", "v_cover_pages_latest"),
                ("xbrl_segment_facts", "v_segment_facts_latest"),
            )
        }
        after["xbrl_roles"] = _rows(
            warehouse.query(
                "SELECT * REPLACE (lower(hex(_row_hash)) AS _row_hash) FROM xbrl_roles FINAL "
                "ORDER BY _row_hash"
            )
        )
        assert after == before
        assert before["xbrl_segment_facts"][0]["fact_id"] == segment.fact_id
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()
        import shutil

        shutil.rmtree(old_ddl, ignore_errors=True)


def test_upgrade_maps_non_hex_ids_like_new_writes(clickhouse_settings: Settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import hash_bytes

    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_test_slim_{uuid4().hex[:12]}"
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    try:
        warehouse.apply_ddl(OLD_DDL)
        warehouse.client.command(
            f"INSERT INTO {database}.xbrl_facts (fact_id, context_hash, accession) "
            f"VALUES ('rev', '', 'a'), ('{_h('x')}', '{_h('ctx')}', 'b')"
        )
        warehouse.upgrade_ddl(Path("sql/clickhouse"))
        rows = warehouse.query(
            "SELECT accession, fact_id, context_hash FROM xbrl_facts ORDER BY accession"
        )
        assert [(row["fact_id"], row["context_hash"]) for row in rows] == [
            (hash_bytes("rev"), bytes(32)),
            (bytes.fromhex(_h("x")), bytes.fromhex(_h("ctx"))),
        ]
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()


def test_filer_labels_never_leak_to_another_filing(clickhouse_settings: Settings) -> None:
    """Two filers label the same us-gaap concept differently; each sees its own labels."""
    from datetime import timedelta

    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import taxonomy_concepts_table

    apple = _bundle()

    def relabel(concept: XbrlConcept) -> XbrlConcept:
        if concept.name != "Revenues":
            return concept
        return concept.model_copy(
            update={
                "labels": (
                    ConceptLabel(
                        role_uri="http://www.xbrl.org/2003/role/terseLabel",
                        language="en-US",
                        text="Total revenue (Other Corp)",
                    ),
                )
            }
        )

    other = apple.model_copy(
        update={
            "filing": apple.filing.model_copy(
                update={"accession": "0000789019-26-000001", "cik": 789019}
            ),
            "concepts": tuple(relabel(concept) for concept in apple.concepts),
            "metadata": apple.metadata.model_copy(
                update={"extracted_at": apple.metadata.extracted_at + timedelta(days=1)}
            ),
        }
    )
    assert {row["labels_rank"] for row in taxonomy_concepts_table(other).to_pylist()} == {0, 1}
    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_test_slim_{uuid4().hex[:12]}"
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    try:
        warehouse.apply_ddl(Path("sql/clickhouse"))
        for bundle in (apple, other):
            warehouse.publish_xbrl_bundle(bundle, warehouse.insert_xbrl_bundle(bundle))
        rows = warehouse.query(
            "SELECT accession, labels_json FROM v_filing_concepts_latest "
            "WHERE concept_name = 'Revenues' ORDER BY accession"
        )
        labels = {row["accession"]: row["labels_json"] for row in rows}
        assert "Total revenue (Other Corp)" in labels["0000789019-26-000001"]
        assert "Other Corp" not in labels[ACCESSION]
        assert labels[ACCESSION] == next(
            row["labels_json"]
            for row in silver_concepts_table(apple).to_pylist()
            if row["concept_name"] == "Revenues"
        )
        # Taxonomy-owned labels (no filer label arc) are still shared once.
        assert warehouse.query(
            "SELECT count() AS n FROM taxonomy_concepts FINAL WHERE labels_json LIKE '%Other Corp%'"
        ) == [{"n": 0}]
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()


def test_upgrade_label_provenance_stays_bounded_with_large_metadata(
    clickhouse_settings: Settings,
) -> None:
    """Real `metadata_json` embeds Arelle's validation log (about 1 MB per extraction).

    The label-provenance check once joined that string onto every label arc and needed
    over 4 GiB to upgrade 43 AAPL filings. It must stay small however large the
    metadata and however many label arcs there are.
    """
    admin = ClickHouseWarehouse(clickhouse_settings)
    database = f"xbrl_test_slim_{uuid4().hex[:12]}"
    assert database.startswith("xbrl_test_")
    admin.client.command(f"CREATE DATABASE {database}")
    warehouse = ClickHouseWarehouse(
        clickhouse_settings.model_copy(update={"clickhouse_database": database})
    )
    try:
        warehouse.apply_ddl(OLD_DDL)
        bundle = _bundle()
        label = next(
            r for r in bundle.relationships if r.relationship_type == RelationshipType.LABEL
        )
        arcs = tuple(
            label.model_copy(update={"relationship_id": _h(f"label-arc-{index}")})
            for index in range(2_000)
        )
        heavy = bundle.model_copy(
            update={
                "relationships": bundle.relationships + arcs,
                "metadata": bundle.metadata.model_copy(
                    update={"provenance": {"validation_log": "x" * 2_000_000}}
                ),
            }
        )
        for table_name, table in (
            ("xbrl_contexts", contexts_table(heavy)),
            ("xbrl_concepts", silver_concepts_table(heavy)),
            ("xbrl_facts", silver_facts_table(heavy)),
            ("xbrl_relationships", relationships_table(heavy)),
            ("xbrl_footnotes", footnotes_table(heavy)),
        ):
            _insert_old(warehouse, table_name, table)
        warehouse.client.insert(
            f"{database}.xbrl_extractions",
            [[ACCESSION, 1, "{}", heavy.metadata.model_dump_json(), datetime.now(UTC)]],
            column_names=[
                "accession",
                "extraction_revision",
                "entity_counts_json",
                "metadata_json",
                "completed_at",
            ],
        )

        warehouse.upgrade_ddl(Path("sql/clickhouse"))

        assert warehouse.query("SELECT concept_qname FROM xbrl_concept_labels") == [
            {"concept_qname": "us-gaap:Revenues"}
        ]
        warehouse.client.command("SYSTEM FLUSH LOGS")
        peaks = warehouse.query(
            "SELECT max(memory_usage) AS peak FROM system.query_log "
            "WHERE type = 'QueryFinish' AND current_database = %(db)s "
            "AND (query LIKE %(taxonomy)s OR query LIKE %(labels)s)",
            {
                "db": database,
                "taxonomy": f"INSERT INTO {database}.taxonomy_concepts%",
                "labels": f"INSERT INTO {database}.xbrl_concept_labels%",
            },
        )
        assert 0 < int(peaks[0]["peak"]) < 256 * 1024**2
    finally:
        warehouse.close()
        admin.client.command(f"DROP DATABASE IF EXISTS {database} SYNC")
        admin.close()
