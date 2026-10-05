import os
from datetime import date
from decimal import Decimal

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.canonical import CanonicalMetric
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact, XbrlUnit
from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_XBRL_INTEGRATION") != "1",
        reason="Set RUN_XBRL_INTEGRATION=1 after starting Docker services",
    ),
]


async def test_mapping_specificity_and_review_workflow(postgres_settings: Settings) -> None:
    repository = PostgresMappingRepository(postgres_settings)
    metric = CanonicalMetric(
        code="integration_revenue",
        name="Integration Revenue",
        statement="income_statement",
        period_type="duration",
        unit_family="monetary",
    )
    namespace = "http://fasb.org/us-gaap/2025"
    try:
        await repository.seed_metrics((metric,))
        global_mapping = await repository.approve_mapping(
            metric=metric.code,
            namespace=namespace,
            concept_name="Revenues",
            explanation="global",
            created_by="integration-test",
        )
        taxonomy_mapping = await repository.approve_mapping(
            metric=metric.code,
            namespace=namespace,
            concept_name="Revenues",
            taxonomy_version="2025",
            explanation="taxonomy",
            created_by="integration-test",
        )
        cik_mapping = await repository.approve_mapping(
            metric=metric.code,
            namespace=namespace,
            concept_name="Revenues",
            cik=1,
            explanation="issuer-specific",
            created_by="integration-test",
        )

        issuer = await repository.approved_mappings_for_concept(
            namespace, "Revenues", "2025", 1, date(2026, 2, 15)
        )
        taxonomy = await repository.approved_mappings_for_concept(
            namespace, "Revenues", "2025", 2, date(2026, 2, 15)
        )
        fallback = await repository.approved_mappings_for_concept(
            namespace, "Revenues", "2024", 2, date(2026, 2, 15)
        )
        assert [item.id for item in issuer] == [cik_mapping.id]
        assert [item.id for item in taxonomy] == [taxonomy_mapping.id]
        assert [item.id for item in fallback] == [global_mapping.id]

        filing = FilingRef(
            accession="0000000001-26-000099",
            cik=1,
            company_name="Example Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/99",
        )
        fact = XbrlFact(
            fact_id="f" * 64,
            concept_namespace=namespace,
            concept_name="Revenues",
            concept_qname="us-gaap:Revenues",
            taxonomy_version="2025",
            context_id="c1",
            context_hash="c" * 64,
            unit=XbrlUnit(unit_id="USD", numerator_measures=("iso4217:USD",)),
            value_kind=FactValueKind.NUMERIC,
            raw_value="1",
            value_decimal=Decimal("1"),
            period_kind=PeriodKind.DURATION,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            entity_scheme="https://www.sec.gov/CIK",
            entity_identifier="1",
            dimension_hash="d" * 64,
            duplicate_set_hash="s" * 64,
            source_document="filing.htm",
        )
        review = await repository.create_review(
            filing=filing,
            fact=fact,
            candidates=(metric.code,),
            evidence={"reason": "test"},
        )
        replay = await repository.create_review(
            filing=filing,
            fact=fact,
            candidates=(metric.code,),
            evidence={"reason": "test"},
        )
        assert replay == review
        assert len(await repository.list_reviews()) == 1
        await repository.resolve_review(review, cik_mapping)
        assert await repository.list_reviews() == []
        # Reviews are per concept: a later filing updates the open review, and
        # replaying a filing doesn't inflate its cumulative counts.
        requests = [
            (fact.model_copy(update={"fact_id": str(i)}), (), {"reason": "bulk"})
            for i in range(1001)
        ]
        first = await repository.create_reviews(filing, requests)
        replay = await repository.create_reviews(filing, requests)
        assert first == replay
        assert len(first) == 1001 and len(set(first.values())) == 1
        later = filing.model_copy(update={"accession": "0000000001-26-000100"})
        await repository.create_reviews(
            later,
            [
                (fact, (), {"reason": "bulk", "fact_count": 3, "sample_fact_ids": ["a", "b"]}),
                (
                    fact.model_copy(
                        update={"concept_namespace": "http://example.com/2025", "fact_id": "x"}
                    ),
                    (),
                    {},
                ),
            ],
        )
        reviews = await repository.list_reviews()
        assert len(reviews) == 2
        revenue = next(item for item in reviews if item["concept_namespace"] == namespace)
        assert revenue["id"] == str(next(iter(first.values())))
        assert (revenue["fact_count"], revenue["filing_count"]) == (1004, 2)
        assert revenue["latest_accession"] == later.accession
    finally:
        await repository.close()


async def test_approval_is_idempotent_and_family_mappings_resolve_reviews(
    postgres_settings: Settings,
) -> None:
    repository = PostgresMappingRepository(postgres_settings)
    metric = CanonicalMetric(
        code="idempotent_net_income",
        name="Net Income",
        statement="income_statement",
        period_type="duration",
        unit_family="monetary",
    )
    try:
        await repository.seed_metrics((metric,))
        arguments = {
            "metric": metric.code,
            "namespace": "us-gaap",
            "concept_name": "NetIncomeLossIdempotent",
            "explanation": "family approval",
            "created_by": "integration",
        }
        first, created = await repository.approve_mapping_once(**arguments)
        again, created_again = await repository.approve_mapping_once(**arguments)
        assert created and not created_again
        assert (again.id, again.version) == (first.id, 1)
        reviewed, created_new = await repository.approve_mapping_once(
            **{**arguments, "explanation": "re-reviewed"}
        )
        assert created_new and reviewed.version == 2
        found = await repository.approved_mappings_for_concept(
            "http://fasb.org/us-gaap/2018-01-31",
            "NetIncomeLossIdempotent",
            "2018-01-31",
            1,
            date(2018, 5, 1),
        )
        assert [item.version for item in found] == [2]

        filing = FilingRef(
            accession="0000000001-26-000123",
            cik=1,
            company_name="Example Corp",
            form="10-K",
            filed_date=date(2026, 2, 15),
            archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/123",
        )
        fact = XbrlFact(
            fact_id="n" * 64,
            concept_namespace="http://fasb.org/us-gaap/2025",
            concept_name="NetIncomeLossIdempotent",
            concept_qname="us-gaap:NetIncomeLossIdempotent",
            taxonomy_version="2025",
            context_id="c1",
            context_hash="c" * 64,
            unit=XbrlUnit(unit_id="USD", numerator_measures=("iso4217:USD",)),
            value_kind=FactValueKind.NUMERIC,
            raw_value="1",
            value_decimal=Decimal("1"),
            period_kind=PeriodKind.DURATION,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            entity_scheme="https://www.sec.gov/CIK",
            entity_identifier="1",
            dimension_hash="d" * 64,
            duplicate_set_hash="s" * 64,
            source_document="filing.htm",
        )
        review = await repository.create_review(
            filing=filing, fact=fact, candidates=(metric.code,), evidence={}
        )
        await repository.resolve_review(review, reviewed)
    finally:
        await repository.close()
