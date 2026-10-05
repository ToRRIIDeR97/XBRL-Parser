"""Independent verification tests for 07-companies-table."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "submissions_company.json"


def _payload(**updates: object) -> dict[str, object]:
    payload = json.loads(FIXTURE.read_text())
    payload.update(updates)
    return payload


# AC-1 boundaries: sparse and oddly typed submissions documents.
def test_ac1_sparse_payload_keeps_unknowns_null() -> None:
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    sparse = {"cik": "0000000042", "name": "Shell Co", "filings": {"recent": {}, "files": []}}
    company = parse_company(sparse)
    assert company.cik == 42
    assert company.tickers == ()
    assert company.exchanges == ()
    assert company.sic is None
    assert company.fiscal_year_end is None
    assert company.former_names == ()


@pytest.mark.parametrize("value", ["1231", None, ""])
def test_ac1_fiscal_year_end_is_mmdd_or_null(value) -> None:
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    company = parse_company(_payload(fiscalYearEnd=value))
    assert company.fiscal_year_end == (value or None)


def test_ac1_malformed_fiscal_year_end_is_rejected_not_stored() -> None:
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    company = parse_company(_payload(fiscalYearEnd="1332"))
    assert company.fiscal_year_end is None


# AC-2/AC-4: re-upsert updates in place; tickers move between companies.
@pytest.mark.integration
def test_ac4_ticker_reassignment_moves_lookup(postgres_settings) -> None:
    from sec_xbrl_warehouse.infrastructure.storage.postgres import PostgresControlRepository
    from sec_xbrl_warehouse.ingestion.filing_discovery import parse_company

    first = parse_company(_payload(cik="1001", name="Old Holder", tickers=["XYZ"], formerNames=[]))
    second = parse_company(_payload(cik="1002", name="New Holder", tickers=["XYZ", "XYZW"], formerNames=[]))
    renamed = parse_company(_payload(cik="1001", name="Old Holder Renamed", tickers=[], formerNames=[]))

    async def run():
        control = PostgresControlRepository(postgres_settings)
        try:
            await control.upsert_company(first)
            await control.upsert_company(second)
            holder = await control.find_company_by_ticker("XYZ")
            old = await control.get_company(1001)
            await control.upsert_company(renamed)
            again = await control.get_company(1001)
            missing = await control.find_company_by_ticker("NOPE")
            return holder, old, again, missing
        finally:
            await control.close()

    holder, old, again, missing = asyncio.run(run())
    assert holder.cik == 1002
    assert "XYZ" not in old.tickers
    assert again.name == "Old Holder Renamed"
    assert missing is None


# AC-5: the worker's discovery handler records companies for every CIK it sees.
async def test_ac5_daily_discovery_enriches_unknown_ciks_once() -> None:
    from datetime import date

    from sec_xbrl_warehouse.ingestion.filing_discovery import enrich_companies

    class _Http:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_json(self, url: str):
            self.urls.append(url)
            return _payload()

    class _Control:
        def __init__(self) -> None:
            self.known = {320193}
            self.saved: list = []

        async def known_company_ciks(self, ciks):
            return {cik for cik in ciks if cik in self.known}

        async def upsert_company(self, company) -> None:
            self.saved.append(company)

    http, control = _Http(), _Control()
    count = await enrich_companies(control, http, ciks=[320193, 789019, 789019], as_of=date(2026, 10, 5))  # type: ignore[arg-type]
    assert count == 1
    assert len(http.urls) == 1 and "CIK0000789019" in http.urls[0]
