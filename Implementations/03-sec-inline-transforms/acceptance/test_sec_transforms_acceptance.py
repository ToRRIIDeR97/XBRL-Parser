"""Worker-visible acceptance tests for 03-sec-inline-transforms."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import FactValueKind
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle

pytestmark = pytest.mark.arelle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> XbrlExtractionBundle:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    package = tmp_path_factory.mktemp("package")
    for path in (FIXTURES / "sec-transforms").iterdir():
        shutil.copy(path, package / path.name)
    settings = Settings(
        env="test",
        arelle_cache_dir=Path("data/cache/arelle"),
        arelle_taxonomy_packages_dir=Path("data/taxonomy-packages"),
    )
    filing = FilingRef(
        accession="0000000001-26-000001",
        cik=1,
        company_name="Example Corp",
        form="10-K",
        filed_date=date(2026, 2, 15),
        primary_document="example-20251231.htm",
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    return ArelleExtractor(settings).extract(package / "example-20251231.htm", filing)


def _fact(bundle: XbrlExtractionBundle, name: str):
    return next(fact for fact in bundle.facts if fact.concept_name == name)


# AC-1
def test_ac1_ballot_box_checkboxes_become_booleans(bundle: XbrlExtractionBundle) -> None:
    annual = _fact(bundle, "AnnualReport")
    quarterly = _fact(bundle, "QuarterlyReport")
    assert (annual.value_kind, annual.value_boolean) == (FactValueKind.BOOLEAN, True)
    assert (quarterly.value_kind, quarterly.value_boolean) == (FactValueKind.BOOLEAN, False)


# AC-1
def test_ac1_sec_code_transforms_produce_codes(bundle: XbrlExtractionBundle) -> None:
    assert _fact(bundle, "StateOfIncorporation").value_text == "CA"
    assert _fact(bundle, "SecurityExchange").value_text == "NASDAQ"


# AC-2
def test_ac2_sec_numeric_transforms_keep_numeric_values(bundle: XbrlExtractionBundle) -> None:
    shares = _fact(bundle, "Shares")
    assert shares.value_kind == FactValueKind.NUMERIC
    assert shares.value_decimal == Decimal("12000")


# AC-3
def test_ac3_known_sec_transforms_raise_no_invalid_transformation(
    bundle: XbrlExtractionBundle,
) -> None:
    flagged = {
        issue.message.split()[1]
        for issue in bundle.validation_issues
        if issue.code.endswith(":invalidTransformation")
    }
    assert flagged <= {"ex:Bogus"}
