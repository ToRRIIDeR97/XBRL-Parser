"""Independent verification tests for 06-canonical-metrics-expansion."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine import make_url

from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper

CONFIG = Path(__file__).resolve().parents[3] / "sec-xbrl-warehouse" / "config"


class _NoRepository:
    async def approved_mappings_for_concept(self, *args, **kwargs):
        return ()

    async def create_reviews(self, filing, reviews):
        return {}


def _mapper() -> CanonicalMapper:
    return CanonicalMapper.load_rules(
        CONFIG / "canonical_metrics.yml", CONFIG / "concept_seed.yml", _NoRepository()
    )


# AC-2: seeds are internally consistent and cover IFRS for the core metrics.
def test_ac2_seed_rules_are_consistent_and_cover_ifrs_core() -> None:
    mapper = _mapper()
    by_concept: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, metrics in mapper.candidate_rules.items():
        by_concept[key].update(metrics)
        assert key[0] in {"us-gaap", "ifrs-full", "dei", "srt"}, key
        assert set(metrics) <= set(mapper.metrics), key
    ambiguous = {key: metrics for key, metrics in by_concept.items() if len(metrics) > 1}
    assert not ambiguous, ambiguous
    ifrs = {metric for (hint, _), metrics in mapper.candidate_rules.items() if hint == "ifrs-full" for metric in metrics}
    assert {"revenue", "net_income", "total_assets", "operating_cash_flow"} <= ifrs


# AC-2: derived metrics declare a formula over known metrics and have no concept seeds.
def test_ac2_derived_metrics_are_formulas_only() -> None:
    mapper = _mapper()
    seeded = {metric for metrics in mapper.candidate_rules.values() for metric in metrics}
    derived = {code for code, metric in mapper.metrics.items() if metric.formula}
    assert "free_cash_flow" in derived
    for code in derived:
        metric = mapper.metrics[code]
        assert metric.statement == "derived", code
        assert code not in seeded, code
        operands = {token for token in metric.formula.replace("(", " ").replace(")", " ").split() if token.isidentifier()}
        assert operands <= set(mapper.metrics), (code, operands - set(mapper.metrics))


async def _repository(settings):
    from sec_xbrl_warehouse.mapping.postgres_repository import PostgresMappingRepository

    repository = PostgresMappingRepository(settings)
    await repository.seed_metrics(tuple(_mapper().metrics.values()))
    return repository


# AC-3: an exact namespace mapping is more specific than a family mapping.
@pytest.mark.integration
def test_ac3_exact_namespace_beats_family(postgres_settings) -> None:
    async def run():
        repository = await _repository(postgres_settings)
        try:
            await repository.approve_mapping(
                metric="revenue", namespace="us-gaap", concept_name="SalesRevenueNet",
                explanation="family", created_by="verifier",
            )
            await repository.approve_mapping(
                metric="gross_profit", namespace="http://fasb.org/us-gaap/2017", concept_name="SalesRevenueNet",
                explanation="deliberately odd exact override", created_by="verifier",
            )
            exact = await repository.approved_mappings_for_concept(
                "http://fasb.org/us-gaap/2017", "SalesRevenueNet", None, 1, date(2017, 5, 1)
            )
            other_year = await repository.approved_mappings_for_concept(
                "http://fasb.org/us-gaap/2016", "SalesRevenueNet", None, 1, date(2016, 5, 1)
            )
            return [m.canonical_metric for m in exact], [m.canonical_metric for m in other_year]
        finally:
            await repository.close()

    assert asyncio.run(run()) == (["gross_profit"], ["revenue"])


# AC-3: family matching is strict about standard namespace URIs.
@pytest.mark.integration
@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        ("http://fasb.org/us-gaap/2009-01-31", 1),
        ("http://example.com/us-gaap-extension/2025", 0),
        ("http://fasb.org/us-gaap-like/2025", 0),
        ("https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full", 0),
    ],
)
def test_ac3_family_matching_is_strict(postgres_settings, namespace: str, expected: int) -> None:
    async def run() -> int:
        repository = await _repository(postgres_settings)
        try:
            await repository.approve_mapping(
                metric="net_income", namespace="us-gaap", concept_name="NetIncomeLoss",
                explanation="family", created_by="verifier",
            )
            return len(
                await repository.approved_mappings_for_concept(namespace, "NetIncomeLoss", None, 1, date(2025, 1, 1))
            )
        finally:
            await repository.close()

    assert asyncio.run(run()) == expected


# AC-5: re-mapping is enqueued per published filing, idempotent within one mapping epoch.
@pytest.mark.integration
def test_ac5_remap_enqueues_one_job_per_filing_per_epoch(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
    from sec_xbrl_warehouse.mapping.remap import enqueue_canonical_remap

    filing = FilingRef(
        accession="0000320193-25-000079", cik=320193, company_name="Apple Inc.", form="10-K",
        filed_date=date(2025, 10, 31),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
    )
    unpublished = filing.model_copy(update={"accession": "0000320193-25-000080"})

    async def run():
        control = PostgresControlRepository(postgres_settings)
        try:
            await control.upsert_filing(filing)
            await control.upsert_filing(unpublished)
            manifest = FilingPackageManifest(filing=filing, members=(), acquired_at=datetime.now(UTC))
            await control.update_filing_package(manifest, 2)
            counts = [
                await enqueue_canonical_remap(control, epoch="mappings-v1"),
                await enqueue_canonical_remap(control, epoch="mappings-v1"),
                await enqueue_canonical_remap(control, epoch="mappings-v2"),
            ]
        finally:
            await control.close()
        url = make_url(postgres_settings.postgres_dsn).set(drivername="postgresql")
        connection = await asyncpg.connect(url.render_as_string(hide_password=False))
        try:
            rows = await connection.fetch(
                "SELECT payload FROM ingestion_jobs WHERE job_type = 'map_canonical' ORDER BY created_at"
            )
        finally:
            await connection.close()
        return counts, rows

    counts, rows = asyncio.run(run())
    assert counts == [1, 0, 1]
    assert len(rows) == 2
    import json

    payloads = [json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"] for row in rows]
    assert {(p["accession"], int(p["extraction_revision"])) for p in payloads} == {("0000320193-25-000079", 2)}
    assert all("facts" not in p for p in payloads)
