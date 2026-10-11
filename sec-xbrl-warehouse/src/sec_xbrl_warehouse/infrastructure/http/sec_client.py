from __future__ import annotations

# Kept so `sec_client.asyncio.sleep` stays patchable for callers and tests that
# predate the move of the request loop into `source_client`.
import asyncio  # noqa: F401
from collections.abc import Iterable
from datetime import datetime

import httpx

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.sources import SEC_SOURCE
from sec_xbrl_warehouse.exceptions import SecAccessError, SecRetryDeferred, SourceRetryDeferred
from sec_xbrl_warehouse.infrastructure.http.egress_lock import EgressLock
from sec_xbrl_warehouse.infrastructure.http.source_client import SourceHttpClient
from sec_xbrl_warehouse.sources.base import SourcePolicy


def sec_policy(settings: Settings, allowed_hosts: Iterable[str] | None = None) -> SourcePolicy:
    """The SEC's source policy: SEC hosts, `XBRL_SEC_*` rate and concurrency, user agent."""
    return SourcePolicy(
        code=SEC_SOURCE,
        allowed_hosts=(
            frozenset(allowed_hosts) if allowed_hosts is not None else SecHttpClient.ALLOWED_HOSTS
        ),
        max_requests_per_second=settings.sec_max_requests_per_second,
        max_concurrency=settings.sec_max_concurrency,
        headers={
            "User-Agent": settings.sec_user_agent,
            "Accept-Encoding": "gzip, deflate",
            "Accept": "*/*",
        },
    )


class SecHttpClient(SourceHttpClient):
    """Rate-limited HTTP client for `sec.gov` and `data.sec.gov` (the SEC policy)."""

    ALLOWED_HOSTS = frozenset({"www.sec.gov", "sec.gov", "data.sec.gov"})
    error = SecAccessError

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        *,
        allowed_hosts: Iterable[str] | None = None,
        egress_lock: EgressLock | None = None,
    ) -> None:
        settings.assert_sec_identity()
        # Taxonomy package sync passes the taxonomy host allowlist; everything
        # else uses the SEC hosts. Rate limits, identity, and byte limits apply alike.
        super().__init__(
            settings,
            sec_policy(settings, allowed_hosts),
            client,
            egress_lock=egress_lock,
        )

    def _check_identity(self) -> None:
        self.settings.assert_sec_identity()

    def _deferred(self, retry_at: datetime) -> SourceRetryDeferred:
        return SecRetryDeferred(retry_at)


def build_archive_directory_url(cik: int, accession: str) -> str:
    accession_digits = accession.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession_digits}"
