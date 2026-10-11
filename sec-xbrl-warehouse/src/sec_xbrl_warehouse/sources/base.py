"""The contract every source (regulator) adapter implements.

An adapter owns everything source-specific about a filing: how filings are
discovered, how a package is downloaded (through its own `SourceHttpClient` and
egress lock), and how filing-level fiscal fields are derived from the extraction.
Storage, extraction, validation, and publishing stay source-neutral.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from sec_xbrl_warehouse.domain.models import (
    Company,
    FilingPackageManifest,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.sources import is_source_code
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle


@dataclass(frozen=True)
class SourcePolicy:
    """What one source's HTTP client may reach, how fast, and with which headers."""

    code: str
    allowed_hosts: frozenset[str]
    max_requests_per_second: float
    max_concurrency: int
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not is_source_code(self.code):
            raise ValueError(f"Invalid source code: {self.code!r}")
        if self.max_requests_per_second <= 0 or self.max_concurrency <= 0:
            raise ValueError(f"Source {self.code} needs a positive rate and concurrency")


def check_disjoint_hosts(policies: Iterable[SourcePolicy]) -> None:
    """Refuse policies whose host allowlists overlap (or that repeat a source code).

    One source's client must never reach another source's hosts, so the SEC policy
    and every other source's policy share no host.
    """
    owners: dict[str, str] = {}
    codes: set[str] = set()
    for policy in policies:
        if policy.code in codes:
            raise ValueError(f"Source {policy.code} has more than one policy")
        codes.add(policy.code)
        for host in policy.allowed_hosts:
            owner = owners.setdefault(host.lower(), policy.code)
            if owner != policy.code:
                raise ValueError(f"Sources {owner} and {policy.code} both allow host {host}")


@dataclass(frozen=True)
class DiscoveryRequest:
    """One discovery run: a filing-date window and/or one issuer.

    `entity` is the source's own issuer identifier as text (SEC: the CIK digits).
    Empty `forms` means the adapter applies no form filter.
    """

    filed_from: date | None = None
    filed_to: date | None = None
    entity: str | None = None
    forms: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryResult:
    """Filings to fetch and the companies the same requests described.

    The worker stores `companies`. An adapter that stores companies itself while it
    discovers (so a retry after a partial failure skips them, as SEC daily-index
    enrichment does) reports how many in `stored_companies`.
    """

    filings: tuple[FilingRef, ...]
    companies: tuple[Company, ...] = ()
    stored_companies: int | None = None


class SourceAdapter(Protocol):
    code: str
    # Scheme-less taxonomy URI prefixes this source's filings use. Informational:
    # `taxonomy_packages.required_prefixes` reads `source_required_prefixes`.
    taxonomy_prefixes: tuple[str, ...]

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult: ...

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest: ...

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]: ...
