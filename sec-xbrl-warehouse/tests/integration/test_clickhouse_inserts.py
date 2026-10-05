import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

pytest.importorskip("clickhouse_connect")
pytest.importorskip("pyarrow")

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalFact
from sec_xbrl_warehouse.domain.enums import SecTable
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


@pytest.fixture(scope="module")
def warehouse(clickhouse_settings: Settings) -> ClickHouseWarehouse:
    instance = ClickHouseWarehouse(clickhouse_settings)
    instance.apply_ddl(Path("sql/clickhouse"))
    yield instance
    instance.close()


def test_sec_bulk_tables_load_and_dedupe(warehouse: ClickHouseWarehouse) -> None:
    """Load a typed batch per SEC table; second load must not change logical counts."""
    fixture = Path(__file__).parents[1] / "fixtures/fsnds-mini.zip"
    from sec_xbrl_warehouse.ingestion.bulk_loader import enrich_sec_batch
    from sec_xbrl_warehouse.ingestion.sec_tsv import SecTsvBatchReader

    reader = SecTsvBatchReader(batch_rows=1)
    artifact_id = "11111111-1111-1111-1111-111111111111"
    period = "2025Q4"
    revision = 1

    for table in SecTable:
        raw = next(reader.iter_batches(fixture, table))
        enriched = enrich_sec_batch(
            table,
            raw,
            artifact_id=artifact_id,
            source_period=period,
            source_revision=revision,
        )
        first = warehouse.insert_sec_batch(table, enriched)
        assert first == enriched.num_rows, table
        # Re-load same logical rows -> ReplacingMergeTree collapses duplicates.
        again = warehouse.insert_sec_batch(table, enriched)
        assert again == enriched.num_rows, table

    # Verify deduplicated logical count equals source row count (1 per table).
    # ReplacingMergeTree merges are asynchronous; use argMax to get the logical
    # latest-revision state without depending on background merges.
    for table in SecTable:
        target = {
            SecTable.SUB: "sec_submissions",
            SecTable.TAG: "sec_tags",
            SecTable.DIM: "sec_dimensions",
            SecTable.NUM: "sec_numeric_facts",
            SecTable.TXT: "sec_text_facts",
            SecTable.REN: "sec_renderings",
            SecTable.PRE: "sec_presentations",
            SecTable.CAL: "sec_calculations",
        }[table]
        rows = warehouse.query(
            f"""SELECT count() AS n FROM (
                    SELECT _row_hash, argMax(_source_revision, _source_revision) AS rev
                    FROM {target}
                    WHERE _artifact_id = %(aid)s
                    GROUP BY _row_hash
                )""",
            {"aid": artifact_id},
        )
        assert rows[0]["n"] == 1, table


def test_xbrl_publication_and_canonical_views_are_replay_safe(
    warehouse: ClickHouseWarehouse, clickhouse_settings: Settings
) -> None:
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    fixture = Path(__file__).parents[1] / "fixtures/xbrl/simple/simple.xml"
    bundle = ArelleExtractor(clickhouse_settings).extract(fixture, filing)
    counts = warehouse.insert_xbrl_bundle(bundle)
    assert counts["facts"] == 3
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_xbrl_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 0
    )
    warehouse.publish_xbrl_bundle(bundle, counts)

    revenue = next(fact for fact in bundle.facts if fact.concept_name == "Revenue")
    canonical = CanonicalFact(
        canonical_fact_id="c" * 64,
        raw_fact_id=revenue.fact_id,
        accession=filing.accession,
        cik=filing.cik,
        filed_date=filing.filed_date,
        form=filing.form,
        canonical_metric="revenue",
        normalized_value_decimal=Decimal("123456789"),
        normalized_unit="USD",
        period_kind="duration",
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        dimensions_json="[]",
        mapping_id=UUID("11111111-1111-1111-1111-111111111111"),
        mapping_version=1,
        mapping_confidence=1,
        mapping_explanation="integration mapping",
        extraction_revision=1,
    )
    assert warehouse.insert_canonical_facts((canonical,)) == 1

    # Replays write physical rows but preserve one logical latest row.
    warehouse.insert_xbrl_bundle(bundle)
    warehouse.publish_xbrl_bundle(bundle, counts)
    warehouse.insert_canonical_facts((canonical,))
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_xbrl_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 3
    )
    assert (
        warehouse.query(
            "SELECT count() AS n FROM v_canonical_facts_latest WHERE accession = %(accession)s",
            {"accession": filing.accession},
        )[0]["n"]
        == 1
    )


def test_statement_lines_are_replay_safe_and_latest_revision_only(
    warehouse: ClickHouseWarehouse,
) -> None:
    from sec_xbrl_warehouse.domain.enums import RelationshipType
    from sec_xbrl_warehouse.domain.xbrl import (
        XbrlDocumentMetadata,
        XbrlExtractionBundle,
        XbrlRelationship,
        XbrlRole,
    )
    from sec_xbrl_warehouse.normalization.statements import PARENT_CHILD_ARCROLE

    role = "http://example.com/role/BalanceSheet"
    bundle = XbrlExtractionBundle(
        filing=FilingRef(
            accession="0000000010-26-000010",
            cik=10,
            company_name="Lines Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/10/10",
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
        concepts=(),
        facts=(),
        relationships=tuple(
            XbrlRelationship(
                relationship_id=f"{index:064d}",
                relationship_type=RelationshipType.PRESENTATION,
                link_role_uri=role,
                arc_role_uri=PARENT_CHILD_ARCROLE,
                from_qname="ex:BalanceSheetAbstract",
                to_qname=f"ex:{child}",
                order=Decimal(index),
            )
            for index, child in enumerate(("Assets", "Liabilities"), start=1)
        ),
        footnotes=(),
        validation_issues=(),
        extraction_revision=1,
        roles=(
            XbrlRole(
                role_uri=role,
                definition="100000 - Statement - Balance Sheets",
                sort_code="100000",
                category="statement",
            ),
        ),
    )
    counts = warehouse.insert_xbrl_bundle(bundle)
    assert (counts["roles"], counts["statement_lines"]) == (1, 3)
    warehouse.publish_xbrl_bundle(bundle, counts)
    warehouse.insert_xbrl_bundle(bundle)  # replay
    revised = bundle.model_copy(update={"extraction_revision": 2})
    revised = revised.model_copy(update={"relationships": revised.relationships[:1]})
    revised_counts = warehouse.insert_xbrl_bundle(revised)
    query = (
        "SELECT extraction_revision, line_number, concept_qname FROM v_statement_lines_latest "
        "WHERE accession = %(accession)s ORDER BY line_number"
    )
    rows = warehouse.query(query, {"accession": bundle.filing.accession})
    assert [row["concept_qname"] for row in rows] == [
        "ex:BalanceSheetAbstract",
        "ex:Assets",
        "ex:Liabilities",
    ]
    warehouse.publish_xbrl_bundle(revised, revised_counts)
    rows = warehouse.query(query, {"accession": bundle.filing.accession})
    assert [(row["extraction_revision"], row["concept_qname"]) for row in rows] == [
        (2, "ex:BalanceSheetAbstract"),
        (2, "ex:Assets"),
    ]
    roles = warehouse.query(
        "SELECT statement_kind FROM xbrl_roles FINAL WHERE accession = %(accession)s",
        {"accession": bundle.filing.accession},
    )
    assert {row["statement_kind"] for row in roles} == {"balance_sheet"}


def test_series_rebuild_versions_priority_and_company_mirror(
    warehouse: ClickHouseWarehouse,
) -> None:
    from datetime import UTC, datetime

    from sec_xbrl_warehouse.domain.models import Company
    from sec_xbrl_warehouse.query.series import rebuild_metric_series
    from sec_xbrl_warehouse.query.service import QueryService

    cik = 555555

    def canonical(accession, accepted, value, concept, raw):
        return CanonicalFact(
            canonical_fact_id=(raw + "c" * 64)[:64],
            raw_fact_id=(raw + "r" * 64)[:64],
            accession=accession,
            cik=cik,
            filed_date=accepted.date(),
            accepted_at=accepted,
            form="10-K",
            canonical_metric="revenue",
            normalized_value_decimal=Decimal(value),
            normalized_unit="USD",
            period_kind="duration",
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            dimensions_json="[]",
            mapping_id=UUID(int=9),
            mapping_version=1,
            mapping_confidence=1.0,
            mapping_explanation="integration",
            extraction_revision=1,
            concept_namespace="http://fasb.org/us-gaap/2024",
            concept_qname=concept,
            fiscal_year=2024,
            fiscal_period="FY",
            report_date=date(2024, 12, 31),
        )

    warehouse.insert_canonical_facts(
        [
            canonical(
                "0000555555-25-000001",
                datetime(2025, 2, 1, tzinfo=UTC),
                100,
                "us-gaap:Revenues",
                "a",
            ),
            canonical(
                "0000555555-25-000001",
                datetime(2025, 2, 1, tzinfo=UTC),
                90,
                "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
                "b",
            ),
            canonical(
                "0000555555-26-000001",
                datetime(2026, 2, 1, tzinfo=UTC),
                105,
                "us-gaap:Revenues",
                "c",
            ),
        ]
    )
    priorities = {
        ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"): 1,
        ("us-gaap", "Revenues"): 2,
    }
    rebuild_metric_series(warehouse, cik, priorities=priorities)
    rebuild_metric_series(warehouse, cik, priorities=priorities)  # replay is idempotent
    service = QueryService(warehouse)

    def annual(**kwargs):
        rows = service.metric_series(cik=cik, metrics=["revenue"], **kwargs)
        return [
            (Decimal(str(row["value"])), row["concept_qname"], row["fiscal_year"]) for row in rows
        ]

    assert annual() == [(Decimal(105), "us-gaap:Revenues", 2024)]
    assert annual(as_of=datetime(2025, 6, 1, tzinfo=UTC)) == [
        (Decimal(90), "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", 2024)
    ]
    assert annual(view="as_reported")[0][0] == Decimal(90)
    assert annual(as_of=datetime(2025, 1, 1, tzinfo=UTC)) == []

    warehouse.upsert_company_mirror(Company(cik=cik, name="Mirror Co", tickers=("mir",)))
    assert service.cik_for_ticker("MIR") == cik
    assert service.cik_for_ticker("NONE") is None
