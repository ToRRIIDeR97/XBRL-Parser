"""A fake second regulator ("demo") used by the 18-source-adapters acceptance tests."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sec_xbrl_warehouse.domain.models import FilingPackageManifest, FilingRef

DEMO_KEY = "demo-REPORT-2024-001"
DEMO_ENTITY = 10_000_000_050


def demo_filing(key: str = DEMO_KEY) -> FilingRef:
    return FilingRef(
        accession=key,
        cik=DEMO_ENTITY,
        company_name="Demo plc",
        form="AFR",
        filed_date=date(2024, 4, 2),
        archive_directory_url="https://api.demo.invalid/filings/REPORT-2024-001",
    )


def sec_filing() -> FilingRef:
    return FilingRef(
        accession="0000320193-25-000079",
        cik=320193,
        company_name="Apple Inc.",
        form="10-K",
        filed_date=date(2025, 10, 31),
        archive_directory_url="https://www.sec.gov/Archives/edgar/data/320193/000032019325000079",
    )


class DemoAdapter:
    """Implements the plan's SourceAdapter contract with canned results."""

    code = "demo"
    taxonomy_prefixes = ("xbrl.demo.invalid/taxonomy/2024/",)

    def __init__(self, filings: tuple[FilingRef, ...] = ()) -> None:
        self.filings = filings
        self.downloaded: list[str] = []
        self.requests: list[object] = []

    async def discover(self, request):
        from sec_xbrl_warehouse.sources.base import DiscoveryResult

        self.requests.append(request)
        return DiscoveryResult(filings=self.filings, companies=())

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        self.downloaded.append(filing.accession)
        return FilingPackageManifest(
            filing=filing,
            members=(),
            acquired_at=datetime.now(UTC),
            package_object_key=f"bronze/demo/filings/{filing.cik}/{filing.accession}/packages/x/package.zip",
        )

    def fiscal_focus(self, bundle):
        return bundle, []


class RecordingSec(DemoAdapter):
    code = "sec"
    taxonomy_prefixes = ()
