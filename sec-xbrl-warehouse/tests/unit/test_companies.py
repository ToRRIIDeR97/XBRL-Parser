import asyncio
from datetime import UTC, date, datetime

from sec_xbrl_warehouse.ingestion.filing_discovery import enrich_companies, parse_company


def test_parallel_tickers_and_exchanges_stay_aligned() -> None:
    company = parse_company(
        {
            "cik": "1",
            "name": "Pairs Corp",
            "tickers": ["PRS", None, "PRS-PA"],
            "exchanges": ["NYSE", "Nasdaq", None],
        }
    )
    assert company.tickers == ("PRS", "PRS-PA")
    assert company.exchanges == ("NYSE", "")


def test_enrichment_refetches_stale_companies_only_with_max_age() -> None:
    class _Http:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get_json(self, url: str):
            self.urls.append(url)
            cik = int(url.rsplit("CIK", 1)[1].split(".")[0])
            return {"cik": str(cik), "name": f"Company {cik}"}

    class _Control:
        def __init__(self) -> None:
            self.saved: list = []
            self.cutoffs: list = []

        async def known_company_ciks(self, ciks):
            return {1, 2} & set(ciks)

        async def stale_company_ciks(self, ciks, fetched_before):
            self.cutoffs.append(fetched_before)
            return {2} & set(ciks)

        async def upsert_company(self, company) -> None:
            self.saved.append(company.cik)

    http, control = _Http(), _Control()
    stored = asyncio.run(
        enrich_companies(control, http, ciks=[1, 2, 3], as_of=date(2026, 10, 5), max_age_days=30)
    )
    assert stored == 2 and control.saved == [2, 3]
    assert control.cutoffs == [datetime(2026, 9, 5, tzinfo=UTC)]

    http, control = _Http(), _Control()
    assert (
        asyncio.run(enrich_companies(control, http, ciks=[1, 2, 3], as_of=date(2026, 10, 5))) == 1
    )
    assert control.saved == [3] and control.cutoffs == []
