from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind, RelationshipType
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

pytestmark = pytest.mark.arelle

FIXTURE = Path(__file__).parents[1] / "fixtures/xbrl/simple/simple.xml"


@pytest.fixture(scope="module")
def bundle() -> object:
    settings = Settings(
        env="test",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
    )
    extractor = ArelleExtractor(settings)
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    return extractor.extract(FIXTURE, filing)


def test_simple_fixture_extracts_core_entities(bundle) -> None:
    from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle

    assert isinstance(bundle, XbrlExtractionBundle)
    assert len(bundle.contexts) == 1
    assert len(bundle.facts) == 3
    assert len(bundle.relationships) >= 1
    assert bundle.extraction_revision == 1


def test_fact_carries_value_and_identity(bundle) -> None:
    fact = next(fact for fact in bundle.facts if fact.concept_name == "Revenue")
    assert fact.concept_qname == "ex:Revenue"
    # Regression: the version comes from the concept namespace, not from the
    # XML Schema namespace of its declaration (which gave "2001").
    assert fact.taxonomy_version == "2026"
    assert fact.concept_name == "Revenue"
    assert fact.value_kind is FactValueKind.NUMERIC
    assert fact.value_decimal == Decimal("123456789")
    assert fact.raw_value == "123456789"
    assert fact.period_kind is PeriodKind.DURATION
    assert fact.period_start == date(2025, 1, 1)
    assert fact.period_end == date(2025, 12, 31)
    assert fact.unit is not None
    assert fact.unit.numerator_measures == ("iso4217:USD",)
    assert fact.entity_identifier == "0000000001"
    assert fact.fact_id and len(fact.fact_id) == 64


def test_context_has_deterministic_hash(bundle) -> None:
    context = bundle.contexts[0]
    assert context.context_id == "D2025"
    assert len(context.context_hash) == 64
    assert context.entity_scheme == "https://www.sec.gov/CIK"
    assert context.period_kind is PeriodKind.DURATION
    assert context.dimensions[0].axis_name == "RegionAxis"
    assert context.dimensions[0].member_name == "UnitedStatesMember"
    assert context.dimensions[0].context_element == "segment"


def test_document_metadata_reports_source(bundle) -> None:
    assert bundle.metadata.entrypoint == "simple.xml"
    assert bundle.metadata.software_name == "arelle"
    assert "http://example.com/2026" in bundle.metadata.taxonomy_namespaces


def test_networks_and_footnotes_keep_fact_lineage(bundle) -> None:
    kinds = {relationship.relationship_type for relationship in bundle.relationships}
    assert {
        RelationshipType.PRESENTATION,
        RelationshipType.CALCULATION,
        RelationshipType.LABEL,
        RelationshipType.FOOTNOTE,
    } <= kinds
    revenue = next(fact for fact in bundle.facts if fact.concept_name == "Revenue")
    assert len(bundle.footnotes) == 1
    assert bundle.footnotes[0].linked_fact_ids == (revenue.fact_id,)
    assert revenue.footnote_ids == (bundle.footnotes[0].footnote_id,)


def test_parser_process_timeout_cleans_up(tmp_path):
    import multiprocessing

    from sec_xbrl_warehouse.exceptions import XbrlExtractionError

    settings = Settings(
        _env_file=None,
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        arelle_timeout_seconds=0.0001,
    )
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Test",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    before = {child.pid for child in multiprocessing.active_children()}
    with pytest.raises(XbrlExtractionError, match="time budget"):
        ArelleExtractor(settings).extract(FIXTURE, filing)
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert not list(settings.temp_dir.glob("arelle-*"))


def test_warm_child_is_reused_and_matches_a_fresh_process(tmp_path) -> None:
    settings = Settings(
        env="test",
        data_dir=tmp_path / "data",
        temp_dir=tmp_path / "tmp",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
    )
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )

    def comparable(item):
        dumped = item.model_dump(mode="json")
        dumped["metadata"].pop("extracted_at")
        return dumped

    warm = ArelleExtractor(settings)
    fresh = ArelleExtractor(settings.model_copy(update={"arelle_child_max_tasks": 1}))
    try:
        first = warm.extract(FIXTURE, filing)
        pid = warm._idle[0].process.pid
        checkpoint = tmp_path / "extraction.json.zst"
        second = warm.extract(FIXTURE, filing, checkpoint_path=checkpoint)
        assert warm._idle[0].process.pid == pid  # the same child ran both
        assert checkpoint.read_bytes()[:4] == b"\x28\xb5\x2f\xfd"  # kept, zstd
        baseline = fresh.extract(FIXTURE, filing)
        assert not fresh._idle  # max_tasks=1: a new process per filing
        assert comparable(first) == comparable(second) == comparable(baseline)
    finally:
        warm.close()
        fresh.close()


def test_child_settings_carry_no_credentials() -> None:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import child_settings

    settings = Settings(
        _env_file=None,
        postgres_dsn="postgresql+asyncpg://w:pg-secret@db/x",
        clickhouse_password="ch-secret",
        object_store_access_key="s3-user",
        object_store_secret_key="s3-secret",
        api_key="k" * 32,
        api_clickhouse_password="reader-secret",
    )
    scrubbed = child_settings(settings)
    values = str(
        [
            scrubbed.postgres_dsn.get_secret_value(),
            scrubbed.clickhouse_password.get_secret_value(),
            scrubbed.object_store_access_key,
            scrubbed.object_store_secret_key.get_secret_value(),
            scrubbed.api_key,
            scrubbed.api_clickhouse_password,
        ]
    )
    for secret in ("pg-secret", "ch-secret", "s3-user", "s3-secret", "k" * 32, "reader-secret"):
        assert secret not in values
    assert scrubbed.sec_user_agent == settings.sec_user_agent
