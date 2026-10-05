"""Independent verification tests for 03-sec-inline-transforms."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle

pytestmark = pytest.mark.arelle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SENTINEL = "(ixTransformValueError)"
SEC_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"


def _settings(**updates: object) -> Settings:
    return Settings(
        env="test",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
        **updates,
    )


def _filing() -> FilingRef:
    return FilingRef(
        accession="0000000003-26-000003",
        cik=3,
        company_name="Verify Corp",
        form="10-K",
        filed_date=date(2026, 3, 1),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/3/3",
    )


def _extract(tmp_path: Path, settings: Settings) -> XbrlExtractionBundle:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    package = tmp_path / "package"
    shutil.copytree(FIXTURES / "sec-transforms", package)
    return ArelleExtractor(settings).extract(package / "example-20251231.htm", _filing())


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> XbrlExtractionBundle:
    return _extract(tmp_path_factory.mktemp("verify"), _settings())


# AC-1 additional transform: country names map to ISO codes.
def test_ac1_country_name_transform(bundle: XbrlExtractionBundle) -> None:
    fact = next(fact for fact in bundle.facts if fact.concept_name == "CountryOfIncorporation")
    assert fact.value_text == "CA"
    assert fact.raw_value == "Canada"


# AC-4: a failed transform never stores the sentinel as data and is still reported.
def test_ac4_unknown_sec_transform_is_reported_not_stored_as_value(
    bundle: XbrlExtractionBundle,
) -> None:
    for fact in bundle.facts:
        assert fact.value_text != SENTINEL, fact.concept_name
    bogus = next(fact for fact in bundle.facts if fact.concept_name == "Bogus")
    assert bogus.value_text is None
    assert bogus.raw_value == "x"
    assert any(
        issue.code.endswith(":invalidTransformation") and "ex:Bogus" in issue.message
        for issue in bundle.validation_issues
    )


# AC-5: SEC transforms load by default; operators don't have to configure plugin paths.
def test_ac5_transforms_do_not_depend_on_operator_plugin_setting(tmp_path: Path) -> None:
    bundle = _extract(tmp_path, _settings(arelle_plugin_paths=""))
    state = next(fact for fact in bundle.facts if fact.concept_name == "StateOfIncorporation")
    assert state.value_text == "CA"


# AC-6: provenance records the transform registry that produced the values.
def test_ac6_extraction_metadata_names_transform_registry(bundle: XbrlExtractionBundle) -> None:
    registries = bundle.metadata.provenance.get("transform_registries")
    assert registries, "provenance must list the loaded transform registries"
    sec = [entry for entry in registries if entry.get("namespace") == SEC_NAMESPACE]
    assert sec and sec[0].get("version"), registries
