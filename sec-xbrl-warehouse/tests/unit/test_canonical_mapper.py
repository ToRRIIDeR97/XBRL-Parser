import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID

from sec_xbrl_warehouse.domain.canonical import CanonicalMetric, ConceptMapping
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlFact, XbrlUnit
from sec_xbrl_warehouse.mapping.canonical_mapper import (
    CanonicalMapper,
    MappingDecision,
)


def _metric() -> CanonicalMetric:
    return CanonicalMetric(
        code="Revenue",
        name="Total Revenue",
        statement="income_statement",
        period_type="duration",
        unit_family="USD",
    )


def _mapping(metric: str = "Revenue", *, version: int = 1, approved: bool = True) -> ConceptMapping:
    return ConceptMapping(
        id=UUID(f"00000000-0000-0000-0000-00000000000{version}"),
        namespace="http://fasb.org/us-gaap",
        concept_name="Revenues",
        canonical_metric=metric,
        mapping_type="exact",
        confidence=1.0,
        approved=approved,
        explanation=f"approved mapping v{version}",
        version=version,
    )


def _fact() -> XbrlFact:
    return XbrlFact(
        fact_id="f1",
        concept_namespace="http://fasb.org/us-gaap",
        concept_name="Revenues",
        concept_qname="us-gaap:Revenues",
        taxonomy_version="2025",
        context_id="c1",
        context_hash="0" * 64,
        unit=XbrlUnit(unit_id="usd", numerator_measures=("iso4217:USD",)),
        value_kind=FactValueKind.NUMERIC,
        raw_value="100",
        value_decimal=Decimal("100"),
        period_kind=PeriodKind.DURATION,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        entity_scheme="https://www.sec.gov/CIK",
        entity_identifier="1",
        dimension_hash="d",
        duplicate_set_hash="s",
        source_document="simple.xml",
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


class _FakeRepository:
    def __init__(self, mappings=(), reviews=None) -> None:
        self.mappings = list(mappings)
        self.reviews = reviews if reviews is not None else []
        self.review_calls = 0
        self.lookup_calls = 0
        self.batch_calls = 0

    async def approved_mappings_for_concept(
        self, namespace, concept_name, taxonomy_version, cik, filed_date
    ):
        self.lookup_calls += 1
        return tuple(m for m in self.mappings if m.approved)

    async def create_reviews(self, filing, reviews):
        self.batch_calls += 1
        return {
            fact.fact_id: await self.create_review(
                filing=filing, fact=fact, candidates=candidates, evidence=evidence
            )
            for fact, candidates, evidence in reviews
        }

    async def create_review(self, *, filing, fact, candidates, evidence) -> UUID:
        self.review_calls += 1
        self.reviews.append((filing, fact, candidates, evidence))
        return UUID("99999999-9999-9999-9999-999999999999")


def test_single_approved_mapping_maps() -> None:
    repo = _FakeRepository(mappings=[_mapping()])
    mapper = CanonicalMapper({"Revenue": _metric()}, {}, repo)
    decision = asyncio.run(mapper.map_fact(_fact(), _filing()))
    assert decision.mapping is not None
    assert decision.mapping.canonical_metric == "Revenue"


def test_multiple_approved_mappings_require_resolution() -> None:
    repo = _FakeRepository(mappings=[_mapping(version=1), _mapping(metric="Revenue2", version=2)])
    mapper = CanonicalMapper({"Revenue": _metric()}, {}, repo)
    decision = asyncio.run(mapper.map_fact(_fact(), _filing()))
    assert decision.mapping is None
    assert set(decision.candidates) == {"Revenue", "Revenue2"}


def test_unmapped_fact_returns_candidates_from_seed() -> None:
    repo = _FakeRepository(mappings=[])
    mapper = CanonicalMapper(
        {"Revenue": _metric()},
        {("us-gaap", "Revenues"): ("Revenue",)},
        repo,
    )
    decision = asyncio.run(mapper.map_fact(_fact(), _filing()))
    assert decision.mapping is None
    assert decision.candidates == ("Revenue",)


def test_map_batch_creates_reviews_for_unmapped() -> None:
    repo = _FakeRepository(mappings=[])
    mapper = CanonicalMapper(
        {"Revenue": _metric()},
        {("us-gaap", "Revenues"): ("Revenue",)},
        repo,
    )
    mapped = asyncio.run(mapper.map_batch([_fact()], _filing(), extraction_revision=1))
    assert mapped == ()
    assert repo.review_calls == 1


def test_to_canonical_fact_carries_lineage() -> None:
    repo = _FakeRepository(mappings=[_mapping()])
    mapper = CanonicalMapper({"Revenue": _metric()}, {}, repo)
    decision = asyncio.run(mapper.map_fact(_fact(), _filing()))
    canonical = mapper.to_canonical_fact(decision, _fact(), _filing(), extraction_revision=3)
    assert canonical.raw_fact_id == "f1"
    assert canonical.canonical_metric == "Revenue"
    assert canonical.mapping_version == 1
    assert canonical.extraction_revision == 3
    assert canonical.normalized_value_decimal == Decimal("100")
    assert canonical.canonical_fact_id


def test_explain_mapping_is_deterministic() -> None:
    repo = _FakeRepository(mappings=[])
    mapper = CanonicalMapper({"Revenue": _metric()}, {}, repo)
    decision = MappingDecision(
        mapping=None,
        candidates=("Revenue",),
        explanation="not mapped",
        evidence={"namespace": "us-gaap"},
    )
    rendered = mapper.explain_mapping(decision)
    assert rendered["mapped"] is False
    assert rendered["candidates"] == ["Revenue"]


def test_load_rules_from_yaml(tmp_path) -> None:
    import yaml

    metrics_path = tmp_path / "metrics.yml"
    seed_path = tmp_path / "seed.yml"
    metrics_path.write_text(
        yaml.safe_dump(
            {
                "metrics": [
                    {
                        "code": "Revenue",
                        "name": "Revenue",
                        "statement": "income_statement",
                        "period_type": "duration",
                        "unit_family": "USD",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    seed_path.write_text(
        yaml.safe_dump(
            {
                "mappings": [
                    {
                        "canonical_metric": "Revenue",
                        "namespace_hint": "us-gaap",
                        "concept": "Revenues",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    mapper = CanonicalMapper.load_rules(metrics_path, seed_path, _FakeRepository())
    assert "Revenue" in mapper.metrics
    assert mapper.candidate_rules[("us-gaap", "Revenues")] == ("Revenue",)


def test_mapping_batches_cache_lookups_but_check_each_facts_period():
    repo = _FakeRepository(mappings=[_mapping()])
    mapper = CanonicalMapper({"Revenue": _metric()}, {}, repo)
    facts = [_fact().model_copy(update={"fact_id": str(i)}) for i in range(1001)]
    facts[-1] = facts[-1].model_copy(update={"period_kind": PeriodKind.INSTANT})
    mapped = asyncio.run(mapper.map_batch(facts, _filing(), extraction_revision=1))
    assert len(mapped) == 1000
    assert repo.lookup_calls == 1
    assert repo.review_calls == 1
    # A new invocation sees newly approved/revoked mappings.
    repo.mappings = []
    assert asyncio.run(mapper.map_batch(facts, _filing(), extraction_revision=1)) == ()
    assert repo.lookup_calls == 2
    assert repo.batch_calls == 4  # one incompatible review, then 500 + 500 + 1


def test_mapping_and_canonical_insert_obey_byte_budget():
    from types import SimpleNamespace

    import pytest

    from sec_xbrl_warehouse.config import Settings
    from sec_xbrl_warehouse.exceptions import WarehouseWriteError
    from sec_xbrl_warehouse.infrastructure.storage.clickhouse import ClickHouseWarehouse

    mapper = CanonicalMapper({"Revenue": _metric()}, {}, _FakeRepository(mappings=[_mapping()]))
    fact = _fact()
    size = len(fact.model_dump_json().encode()) + 512

    async def collect(budget):
        return [
            batch
            async for batch in mapper.map_batches(
                [fact, fact], _filing(), extraction_revision=1, batch_bytes=budget
            )
        ]

    batches = asyncio.run(collect(size))
    assert [len(batch) for batch in batches] == [1, 1]
    with pytest.raises(ValueError, match="byte budget"):
        asyncio.run(collect(size - 1))
    canonical = batches[0][0]
    budget = len(canonical.model_dump_json().encode()) + 512
    warehouse = ClickHouseWarehouse(Settings(arrow_batch_bytes=budget), client=SimpleNamespace())
    counts = []
    warehouse._insert_arrow = lambda name, table: counts.append(table.num_rows) or table.num_rows
    assert warehouse.insert_canonical_facts([canonical, canonical]) == 2
    assert counts == [1, 1]
    warehouse.settings.arrow_batch_bytes = budget - 1
    with pytest.raises(WarehouseWriteError, match="byte budget"):
        warehouse.insert_canonical_facts([canonical])
