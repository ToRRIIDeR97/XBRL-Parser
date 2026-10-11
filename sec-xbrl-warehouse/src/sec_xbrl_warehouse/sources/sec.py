"""The SEC source adapter: today's EDGAR discovery, package download, and dei fiscal focus."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sec_xbrl_warehouse.domain.models import (
    FilingPackageManifest,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.sources import SEC_SOURCE
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import ConfigurationError
from sec_xbrl_warehouse.ingestion.filing_discovery import enrich_companies
from sec_xbrl_warehouse.normalization.fiscal_focus import derive_fiscal_focus
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, DiscoveryResult

_CIK = re.compile(r"[0-9]{1,10}")


class SecSourceAdapter:
    """Wraps `SecSubmissionsDiscovery`, `SecDailyIndexDiscovery`, `FilingPackageDownloader`,
    and `derive_fiscal_focus` without changing what they request or produce.

    Collaborators are optional so parse-only workers (no SEC client) can still
    route fiscal focus here; a missing one raises `ConfigurationError` when used.
    """

    code = SEC_SOURCE
    # The US taxonomy list is `required_prefixes` in `config/taxonomy_packages.yml`.
    taxonomy_prefixes: tuple[str, ...] = ()

    def __init__(
        self,
        downloader: Any = None,
        *,
        submissions: Any = None,
        daily_index: Any = None,
        http: Any = None,
        companies: Any = None,
        data_api_base: str = "https://data.sec.gov",
        company_refresh_days: int | None = None,
    ) -> None:
        self.downloader = downloader
        self.submissions = submissions
        self.daily_index = daily_index
        # Daily indexes carry no company data: with `http` and a `companies` store
        # (`known_company_ciks`, `stale_company_ciks`, `upsert_company`), new or stale
        # CIKs are fetched and stored here, one at a time.
        self.http = http
        self.companies = companies
        self.data_api_base = data_api_base
        self.company_refresh_days = company_refresh_days

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        if request.entity is not None:
            # One issuer from its submissions JSON: the same request yields the company.
            if _CIK.fullmatch(request.entity) is None:
                raise ValueError(f"SEC entity must be CIK digits: {request.entity!r}")
            company, filings = await self._require(self.submissions).discover_with_company(
                int(request.entity),
                forms=request.forms,
                filed_from=request.filed_from,
                filed_to=request.filed_to,
            )
            return DiscoveryResult(filings=tuple(filings), companies=(company,))
        if request.filed_from is None or request.filed_to is None:
            raise ValueError("Daily-index discovery needs filed_from and filed_to")
        filings = await self._require(self.daily_index).discover_range(
            request.filed_from, request.filed_to, request.forms
        )
        if self.http is None or self.companies is None:
            return DiscoveryResult(filings=tuple(filings))
        # Stored one by one as fetched, so a retry after a partial failure skips the
        # CIKs already stored instead of fetching the whole batch again.
        stored = await enrich_companies(
            self.companies,
            self.http,
            ciks=[filing.cik for filing in filings],
            as_of=request.filed_to,
            data_api_base=self.data_api_base,
            max_age_days=self.company_refresh_days,
        )
        return DiscoveryResult(filings=tuple(filings), stored_companies=stored)

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        manifest: FilingPackageManifest = await self._require(self.downloader).download_package(
            filing
        )
        return manifest

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        """Filing fiscal fields from the bundle's dei facts (`derive_fiscal_focus`)."""
        filing, issues = derive_fiscal_focus(bundle)
        return bundle.model_copy(update={"filing": filing}), issues

    @staticmethod
    def _require(collaborator: Any) -> Any:
        if collaborator is None:
            raise ConfigurationError(
                "This worker has no SEC client; run SEC discovery and fetch jobs on a "
                "SEC-enabled worker"
            )
        return collaborator
