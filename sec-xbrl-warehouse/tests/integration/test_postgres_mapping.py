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
    finally:
        await repository.close()
