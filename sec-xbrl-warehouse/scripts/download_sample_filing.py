from __future__ import annotations

import argparse
import asyncio
from datetime import date

from sec_xbrl_warehouse.config import get_settings
from sec_xbrl_warehouse.infrastructure.http.sec_client import SecHttpClient
from sec_xbrl_warehouse.ingestion.filing_discovery import SecSubmissionsDiscovery


async def run(cik: int, form: str) -> None:
    settings = get_settings()
    settings.assert_sec_identity()
    async with SecHttpClient(settings) as http:
        filings = await SecSubmissionsDiscovery(http).discover(
            cik,
            forms=(form,),
            filed_from=date(2020, 1, 1),
            include_older_files=False,
        )
    for filing in filings[-5:]:
        print(filing.model_dump_json(indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Print recent SEC filing references for a CIK")
    parser.add_argument("cik", type=int)
    parser.add_argument("--form", default="10-K")
    args = parser.parse_args()
    asyncio.run(run(args.cik, args.form))


if __name__ == "__main__":
    main()
