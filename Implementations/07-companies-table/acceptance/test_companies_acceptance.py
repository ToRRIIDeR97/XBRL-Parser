"""Worker-visible acceptance tests for 07-companies-table."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "submissions_company.json"


# AC-1
def test_ac1_company_is_parsed_from_submissions_json() -> None:
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    company = parse_company(json.loads(FIXTURE.read_text()))
    assert company.cik == 320193
    assert company.name == "Apple Inc."
    assert company.tickers == ("AAPL",)
    assert company.exchanges == ("Nasdaq",)
    assert company.sic == 3571
    assert company.sic_description == "Electronic Computers"
    assert company.fiscal_year_end == "0927"
    assert company.state_of_incorporation == "CA"
    assert company.entity_type == "operating"
    assert company.filer_category == "Large accelerated filer"
    assert [item.name for item in company.former_names] == ["APPLE INC", "APPLE COMPUTER INC"]
    assert company.former_names[0].valid_to == date(2019, 8, 5)


# AC-3
async def test_ac3_discovery_returns_company_without_extra_requests() -> None:
    from sec_xbrl_warehouse.ingestion.filing_discovery import SecSubmissionsDiscovery

    class _Http:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_json(self, url: str):
            self.urls.append(url)
            return json.loads(FIXTURE.read_text())

    http = _Http()
    company, filings = await SecSubmissionsDiscovery(http).discover_with_company(  # type: ignore[arg-type]
        320193, forms=("10-K",)
    )
    assert company.tickers == ("AAPL",)
    assert [filing.accession for filing in filings] == ["0000320193-25-000079"]
    assert len(http.urls) == 1


# AC-2
@pytest.mark.integration
def test_ac2_companies_persist_and_resolve_by_ticker(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    async def run():
        control = PostgresControlRepository(postgres_settings)
        try:
            await control.upsert_company(parse_company(json.loads(FIXTURE.read_text())))
            return await control.get_company(320193), await control.find_company_by_ticker("aapl")
        finally:
            await control.close()

    by_cik, by_ticker = asyncio.run(run())
    assert by_cik is not None and by_ticker is not None
    assert by_cik.cik == by_ticker.cik == 320193
    assert by_ticker.fiscal_year_end == "0927"
