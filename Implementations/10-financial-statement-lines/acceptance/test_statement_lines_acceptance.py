"""Worker-visible acceptance tests for 10-financial-statement-lines."""

from __future__ import annotations

import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import FilingRef
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle

pytestmark = pytest.mark.arelle

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROLE = "http://example.com/role/"


@pytest.fixture(scope="module")
def bundle(tmp_path_factory: pytest.TempPathFactory) -> XbrlExtractionBundle:
    from sec_xbrl_warehouse.infrastructure.xbrl.arelle_adapter import ArelleExtractor

    package = tmp_path_factory.mktemp("statements") / "package"
    shutil.copytree(FIXTURES / "statements", package)
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
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/1/1",
    )
    return ArelleExtractor(settings).extract(package / "fs-20251231.xml", filing)


# AC-1
def test_ac1_role_definitions_are_extracted(bundle: XbrlExtractionBundle) -> None:
    roles = {role.role_uri: role for role in bundle.roles}
    balance = roles[ROLE + "BalanceSheet"]
    assert balance.definition == "100000 - Statement - Consolidated Balance Sheets"
    assert balance.sort_code == "100000"
    assert balance.category == "statement"
    assert roles[ROLE + "RevenueDetails"].category == "disclosure"


# AC-2
def test_ac2_primary_statements_are_classified(bundle: XbrlExtractionBundle) -> None:
    from sec_xbrl_warehouse.normalization.statements import StatementKind, classify_roles

    assert classify_roles(bundle) == {
        ROLE + "BalanceSheet": StatementKind.BALANCE_SHEET,
        ROLE + "IncomeStatement": StatementKind.INCOME_STATEMENT,
        ROLE + "CashFlow": StatementKind.CASH_FLOW,
    }


# AC-3
def test_ac3_balance_sheet_lines_follow_presentation_with_labels_and_values(
    bundle: XbrlExtractionBundle,
) -> None:
    from sec_xbrl_warehouse.normalization.statements import StatementKind, build_statement_lines

    lines = [
        line
        for line in build_statement_lines(bundle)
        if line.statement_kind == StatementKind.BALANCE_SHEET
    ]
    assert [line.label for line in lines if not line.is_abstract] == [
        "Cash and cash equivalents",
        "Accounts receivable, net",
        "Total current assets",
        "Property, plant and equipment, net",
        "Total assets",
        "Total liabilities",
        "Total shareholders' equity",
        "Total liabilities and shareholders' equity",
    ]
    assert [line.line_number for line in lines] == list(range(1, len(lines) + 1))
    total = next(line for line in lines if line.concept_qname == "fs:Assets")
    assert total.is_total and total.depth == 2
    assert {(value.period_instant, value.value_decimal) for value in total.values} == {
        (date(2025, 12, 31), Decimal("1000000000")),
        (date(2024, 12, 31), Decimal("850000000")),
    }
