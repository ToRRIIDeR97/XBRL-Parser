from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind, PeriodKind
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
    assert len(bundle.facts) == 1
    assert len(bundle.relationships) >= 1
    assert bundle.extraction_revision == 1


def test_fact_carries_value_and_identity(bundle) -> None:
    fact = bundle.facts[0]
    assert fact.concept_qname == "ex:Revenue"
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


def test_document_metadata_reports_source(bundle) -> None:
    assert bundle.metadata.entrypoint == "simple.xml"
    assert bundle.metadata.software_name == "arelle"
    assert "http://example.com/2026" in bundle.metadata.taxonomy_namespaces
