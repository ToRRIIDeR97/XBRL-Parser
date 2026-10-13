"""The ESEF source adapter: EU/UK annual reports from XBRL International's filings.xbrl.org.

filings.xbrl.org serves a JSON:API at `https://filings.xbrl.org/api`. Discovery pages
`/api/filings` newest first; each kept record becomes a `FilingRef` keyed
`esef-<fxo_id>` with an entity key allocated from the issuer's LEI. A package is the
report package zip the site publishes, checked against the SHA-256 the site publishes
and stored byte-identical as the filing's package archive.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import re
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import PeriodKind, Severity
from sec_xbrl_warehouse.domain.models import (
    Company,
    FilingPackageManifest,
    FilingRef,
    PackageMember,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.sources import filing_source, is_filing_key
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
from sec_xbrl_warehouse.exceptions import (
    ArtifactIntegrityError,
    ConfigurationError,
    SchemaDriftError,
    UnsupportedSourceError,
)
from sec_xbrl_warehouse.ingestion.bulk_downloader import validate_zip_integrity
from sec_xbrl_warehouse.ingestion.filing_downloader import (
    _manifest_prefix,
    _safe_package_name,
    filing_package_key,
)
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, DiscoveryResult, SourcePolicy

logger = get_logger(__name__)

ESEF_SOURCE = "esef"
ESEF_FORM = "ESEF"
LEI_SCHEME = "http://standards.iso.org/iso/17442"
FXO_HOST = "filings.xbrl.org"
FXO_BASE = f"https://{FXO_HOST}"

# filings.xbrl.org only; at most 2 requests/s. `esef_policy(settings)` adds the
# warehouse user agent (`XBRL_SEC_USER_AGENT`, the identity every outbound path sends).
ESEF_POLICY = SourcePolicy(
    code=ESEF_SOURCE,
    allowed_hosts=frozenset({FXO_HOST}),
    max_requests_per_second=2.0,
    max_concurrency=2,
    headers={"Accept": "application/vnd.api+json, application/json;q=0.9, */*;q=0.8"},
)

# Scheme-less taxonomy prefixes ESEF filings import: the ESMA ESEF taxonomy and the
# IFRS Accounting Taxonomy years named by the ESEF RTS. Must match
# `source_required_prefixes.esef` in `config/taxonomy_packages.yml`.
ESEF_TAXONOMY_PREFIXES: tuple[str, ...] = (
    "www.esma.europa.eu/taxonomy/2019-03-27/",
    "www.esma.europa.eu/taxonomy/2020-03-16/",
    "www.esma.europa.eu/taxonomy/2021-03-24/",
    "www.esma.europa.eu/taxonomy/2022-03-24/",
    "xbrl.ifrs.org/taxonomy/2019-03-27/",
    "xbrl.ifrs.org/taxonomy/2020-03-16/",
    "xbrl.ifrs.org/taxonomy/2021-03-24/",
    "xbrl.ifrs.org/taxonomy/2022-03-24/",
    "xbrl.ifrs.org/taxonomy/2023-03-23/",
    "xbrl.ifrs.org/taxonomy/2024-03-27/",
)

_LEI = re.compile(r"[A-Z0-9]{18}[0-9]{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPORT_SUFFIXES = (".xhtml", ".html", ".htm")


class EntityKeys(Protocol):
    async def resolve_entity_key(self, scheme: str, identifier: str) -> int: ...


def esef_policy(settings: Settings) -> SourcePolicy:
    """`ESEF_POLICY` with the warehouse user agent."""
    return replace(
        ESEF_POLICY, headers={**ESEF_POLICY.headers, "User-Agent": settings.sec_user_agent}
    )


def normalize_lei(value: object) -> str | None:
    """An LEI without surrounding whitespace, upper-cased; None if it isn't LEI-shaped."""
    if not isinstance(value, str):
        return None
    lei = value.strip().upper()
    return lei if _LEI.fullmatch(lei) else None


class EsefSourceAdapter:
    """Discovery, package download, and fiscal focus for filings.xbrl.org.

    `http` is the source's `SourceHttpClient` (None on parse-only workers, which only
    use `fiscal_focus`); `entity_keys` allocates entity keys (the control repository).
    """

    code = ESEF_SOURCE
    taxonomy_prefixes = ESEF_TAXONOMY_PREFIXES

    def __init__(
        self,
        settings: Settings,
        http: Any,
        object_store: ObjectStore,
        entity_keys: EntityKeys,
    ) -> None:
        self.settings = settings
        self.http = http
        self.object_store = object_store
        self.entity_keys = entity_keys

    # Discovery -------------------------------------------------------------------

    async def discover(self, request: DiscoveryRequest) -> DiscoveryResult:
        """ESEF reports added to filings.xbrl.org in `[filed_from, filed_to]`.

        Pages `/api/filings` newest first (per configured country) and stops once a
        page reaches back before `filed_from`, when there is no next page, or after
        `esef_max_pages` pages.
        """
        entity: str | None = None
        if request.entity is not None:
            entity = normalize_lei(request.entity)
            if entity is None:
                raise ValueError(f"ESEF entity must be an LEI: {request.entity!r}")
        if request.forms and ESEF_FORM not in request.forms:
            return DiscoveryResult(filings=())
        http = self._require_http()
        filings: dict[str, FilingRef] = {}
        companies: dict[int, Company] = {}
        keys: dict[str, int] = {}
        for country in self.settings.esef_countries or (None,):
            for number in range(1, self.settings.esef_max_pages + 1):
                params = {
                    "sort": "-date_added",
                    "include": "entity",
                    "page[size]": str(self.settings.esef_page_size),
                    "page[number]": str(number),
                }
                if country is not None:
                    params["filter[country]"] = country
                page = await http.get_json(f"{FXO_BASE}/api/filings?{urlencode(params)}")
                records = _records(page)
                entities = _entities(page)
                oldest: date | None = None
                for record in records:
                    added = _record_date(record)
                    if added is not None and (oldest is None or added < oldest):
                        oldest = added
                    try:
                        filing = await self._filing(record, entities, request, entity, keys)
                    except _Skip as skipped:
                        _log_skip(record, str(skipped))
                        continue
                    if filing is None or filing.accession in filings:
                        continue
                    filings[filing.accession] = filing
                    companies.setdefault(
                        filing.cik, Company(cik=filing.cik, name=filing.company_name)
                    )
                past_window = (
                    request.filed_from is not None
                    and oldest is not None
                    and oldest < request.filed_from
                )
                links = page.get("links")
                has_next = isinstance(links, dict) and bool(links.get("next"))
                if past_window or not has_next or not records:
                    break
        return DiscoveryResult(filings=tuple(filings.values()), companies=tuple(companies.values()))

    async def _filing(
        self,
        record: dict[str, Any],
        entities: dict[str, dict[str, Any]],
        request: DiscoveryRequest,
        entity: str | None,
        keys: dict[str, int],
    ) -> FilingRef | None:
        """The record as a `FilingRef`; None outside the request, `_Skip` when unsafe."""
        attributes = record.get("attributes")
        if not isinstance(attributes, dict):
            raise _Skip("record has no attributes")
        added = _record_date(record)
        if added is None:
            raise _Skip("missing or invalid date_added")
        if (request.filed_from is not None and added < request.filed_from) or (
            request.filed_to is not None and added > request.filed_to
        ):
            return None
        issuer = entities.get(_entity_id(record) or "", {})
        lei = normalize_lei(issuer.get("identifier"))
        if lei is None:
            raise _Skip("missing or malformed entity LEI")
        if entity is not None and lei != entity:
            return None
        fxo_id = attributes.get("fxo_id")
        accession = f"esef-{fxo_id}" if isinstance(fxo_id, str) else ""
        if not is_filing_key(accession):
            raise _Skip("fxo_id is not a valid filing key")
        package_path = _site_path(attributes.get("package_url"))
        if package_path is None:
            raise _Skip("missing or unsafe package_url")
        report_path = _site_path(attributes.get("report_url"))
        try:
            report_date = _optional_date(attributes.get("period_end"))
        except ValueError:
            raise _Skip("invalid period_end") from None
        if lei not in keys:
            keys[lei] = await self.entity_keys.resolve_entity_key(LEI_SCHEME, lei)
        name = issuer.get("name")
        try:
            return FilingRef(
                accession=accession,
                cik=keys[lei],
                company_name=name.strip() if isinstance(name, str) and name.strip() else lei,
                form=ESEF_FORM,
                filed_date=added,
                report_date=report_date,
                archive_directory_url=FXO_BASE + str(PurePosixPath(package_path).parent),
                primary_document_url=FXO_BASE + report_path if report_path else None,
                is_inline_xbrl=True,
            )
        except ValueError as exc:
            raise _Skip(f"invalid filing reference: {exc}") from None

    # Package download ------------------------------------------------------------

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        """Download, verify, and store the filing's report package.

        The record is read again so the package URL and digest are current. The
        bytes must match the published SHA-256 (nothing is stored otherwise) and pass
        the archive limits; the zip is stored byte-identical at
        `filing_package_key(filing, sha256)`. A verified package that fails the
        archive checks is kept under `quarantine/esef/` for diagnosis.
        """
        if filing_source(filing.accession) != ESEF_SOURCE:
            raise UnsupportedSourceError(
                f"The ESEF adapter can't fetch {filing.source} filing {filing.accession}"
            )
        http = self._require_http()
        fxo_id = filing.accession.removeprefix("esef-")
        page = await http.get_json(
            f"{FXO_BASE}/api/filings?{urlencode({'filter[fxo_id]': fxo_id})}"
        )
        matches = [
            record
            for record in _records(page)
            if isinstance(record.get("attributes"), dict)
            and record["attributes"].get("fxo_id") == fxo_id
        ]
        if len(matches) != 1:
            raise ArtifactIntegrityError(
                f"filings.xbrl.org lists {len(matches)} records for {filing.accession}"
            )
        attributes = matches[0]["attributes"]
        digest = str(attributes.get("sha256") or "").strip().lower()
        if _SHA256.fullmatch(digest) is None:
            raise ArtifactIntegrityError(f"No published SHA-256 for {filing.accession}")
        package_path = _site_path(attributes.get("package_url"))
        if package_path is None:
            raise ArtifactIntegrityError(f"Unsafe package URL for {filing.accession}")
        url = FXO_BASE + package_path
        report_path = _site_path(attributes.get("report_url"))
        report_name = (
            str(PurePosixPath(report_path).relative_to(PurePosixPath(package_path).parent))
            if report_path
            and PurePosixPath(report_path).is_relative_to(PurePosixPath(package_path).parent)
            else None
        )

        cache_key = _manifest_key(filing, digest)
        package_key = filing_package_key(filing, digest)
        cached = await asyncio.to_thread(self._cached_manifest, cache_key, package_key, filing)
        if cached is not None:
            return cached

        self.settings.ensure_directories()
        with tempfile.TemporaryDirectory(
            prefix="esef-download-", dir=self.settings.temp_dir
        ) as directory:
            archive = Path(directory) / "package.zip"
            # Raises ArtifactIntegrityError on a digest mismatch before anything is stored.
            await http.download(
                url, archive, expected_sha256=digest, max_bytes=self.settings.filing_max_bytes
            )
            try:
                members = await asyncio.to_thread(
                    _package_members, archive, self.settings, url, report_name
                )
            except ArtifactIntegrityError:
                await asyncio.to_thread(
                    self.object_store.put_file,
                    archive,
                    f"quarantine/esef/filings/{filing.accession}/sha256={digest}/package.zip",
                    expected_sha256=digest,
                    metadata={"accession": filing.accession, "source-url": url},
                )
                raise
            await asyncio.to_thread(
                self.object_store.put_file,
                archive,
                package_key,
                expected_sha256=digest,
                metadata={"accession": filing.accession, "source-url": url},
            )
        manifest = FilingPackageManifest(
            filing=filing,
            members=members,
            acquired_at=datetime.now(UTC),
            package_object_key=package_key,
            package_checksum_sha256=digest,
        )
        payload = manifest.model_dump_json().encode("utf-8")
        try:
            stored = await asyncio.to_thread(self.object_store.put_bytes, payload, cache_key)
        except ArtifactIntegrityError:
            # A concurrent fetch stored its manifest first; reuse it.
            cached = await asyncio.to_thread(self._cached_manifest, cache_key, package_key, filing)
            if cached is None:
                raise
            return cached
        return manifest.model_copy(
            update={
                "manifest_object_key": cache_key,
                "manifest_checksum_sha256": stored.checksum_sha256,
            }
        )

    def _cached_manifest(
        self, key: str, package_key: str, filing: FilingRef
    ) -> FilingPackageManifest | None:
        """The manifest stored by an earlier fetch of the same package bytes, if any."""
        if not self.object_store.exists(key) or not self.object_store.exists(package_key):
            return None
        with tempfile.TemporaryDirectory(dir=self.settings.temp_dir) as directory:
            path = Path(directory) / "manifest.json"
            stored = self.object_store.get_file(
                key, path, max_bytes=self.settings.response_max_bytes
            )
            manifest = FilingPackageManifest.model_validate_json(path.read_bytes())
        if manifest.filing != filing or manifest.package_object_key != package_key:
            raise ArtifactIntegrityError(f"Cached manifest metadata mismatch: {key}")
        return manifest.model_copy(
            update={"manifest_object_key": key, "manifest_checksum_sha256": stored.checksum_sha256}
        )

    # Fiscal focus ----------------------------------------------------------------

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        """ESEF reports are annual: `FY` and the reporting period's end year.

        Without a report date the year is the end year of the latest
        non-dimensional duration fact.
        """
        filing = bundle.filing
        issues: list[ValidationIssue] = []
        year: int | None = filing.report_date.year if filing.report_date else None
        if year is None:
            ends = [
                fact.period_end
                for fact in bundle.facts
                if fact.period_kind is PeriodKind.DURATION
                and not fact.dimensions
                and not fact.is_nil
                and fact.period_end is not None
            ]
            year = max(ends).year if ends else None
        if year is None:
            issues.append(
                ValidationIssue(
                    source="fiscal_focus",
                    code="warehouse:fiscalYearUnknown",
                    severity=Severity.WARNING,
                    accession=filing.accession,
                    message="No report date or non-dimensional duration fact gives a fiscal year",
                )
            )
        updated = filing.model_copy(update={"fiscal_period": "FY", "fiscal_year": year})
        return bundle.model_copy(update={"filing": updated}), tuple(issues)

    def _require_http(self) -> Any:
        if self.http is None:
            raise ConfigurationError(
                "This worker has no ESEF client; run ESEF discovery and fetch jobs on a "
                "worker that claims them with esef enabled"
            )
        return self.http


def _records(page: dict[str, Any]) -> list[dict[str, Any]]:
    data = page.get("data")
    if not isinstance(data, list):
        raise SchemaDriftError("filings.xbrl.org response has no data list")
    return [record for record in data if isinstance(record, dict)]


def _entities(page: dict[str, Any]) -> dict[str, dict[str, Any]]:
    included = page.get("included") or []
    if not isinstance(included, list):
        raise SchemaDriftError("filings.xbrl.org response has a malformed included list")
    return {
        str(item.get("id")): item["attributes"]
        for item in included
        if isinstance(item, dict)
        and item.get("type") == "entity"
        and isinstance(item.get("attributes"), dict)
    }


def _entity_id(record: dict[str, Any]) -> str | None:
    relationships = record.get("relationships")
    if not isinstance(relationships, dict):
        return None
    entity = relationships.get("entity")
    data = entity.get("data") if isinstance(entity, dict) else None
    if not isinstance(data, dict) or data.get("type") != "entity":
        return None
    return str(data.get("id"))


def _record_date(record: dict[str, Any]) -> date | None:
    attributes = record.get("attributes")
    value = attributes.get("date_added") if isinstance(attributes, dict) else None
    try:
        return _optional_date(value)
    except ValueError:
        return None


def _optional_date(value: object) -> date | None:
    """`YYYY-MM-DD` from a date or timestamp string; None when absent."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Not a date: {value!r}")
    return date.fromisoformat(value.strip()[:10])


def _site_path(value: object) -> str | None:
    """A host-relative filings.xbrl.org path (or a full URL on that host), else None."""
    if not isinstance(value, str) or not value:
        return None
    parts = urlsplit(value)
    if (parts.scheme or parts.netloc) and (
        parts.scheme != "https" or parts.hostname != FXO_HOST or parts.port not in {None, 443}
    ):
        return None
    if parts.query or parts.fragment or not parts.path.startswith("/") or "\\" in parts.path:
        return None
    path = PurePosixPath(parts.path)
    if ".." in path.parts or "." in path.parts or parts.path.startswith("//"):
        return None
    return parts.path


class _Skip(Exception):
    """A filings.xbrl.org record that can't become a safe `FilingRef`."""


def _log_skip(record: dict[str, Any], reason: str) -> None:
    attributes = record.get("attributes")
    logger.warning(
        "esef_record_skipped",
        record_id=record.get("id"),
        fxo_id=attributes.get("fxo_id") if isinstance(attributes, dict) else None,
        reason=reason,
    )


def _manifest_key(filing: FilingRef, digest: str) -> str:
    filing_checksum = hashlib.sha256(
        json.dumps(filing.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{_manifest_prefix(filing)}package-sha256={digest}/filing-sha256={filing_checksum}.json"


def _package_members(
    archive: Path, settings: Settings, url: str, report_name: str | None
) -> tuple[PackageMember, ...]:
    """Validate a report package and describe its entries; one entry is the report.

    Applies the bulk ZIP checks (traversal, member count, expanded bytes, compression
    ratio, sizes) bounded by the filing limits. The entrypoint is the inline XBRL
    report under `reports/`; the published `report_url` decides between several.
    """
    scoped = settings.model_copy(
        update={
            "zip_max_members": min(settings.zip_max_members, settings.filing_max_members),
            "zip_max_total_bytes": min(settings.zip_max_total_bytes, settings.filing_max_bytes),
        }
    )
    validate_zip_integrity(archive, scoped)
    rows: list[tuple[str, str, int]] = []
    with zipfile.ZipFile(archive) as package:
        for info in package.infolist():
            if info.is_dir():
                continue
            name = _safe_package_name(info.filename)
            digest = hashlib.sha256()
            size = 0
            with package.open(info) as source:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            rows.append((name, digest.hexdigest(), size))
    reports = sorted(name for name, _, _ in rows if _is_report(name))
    if len(reports) > 1 and report_name is not None:
        reports = [name for name in reports if name.split("/", 1)[-1] == report_name] or reports
    if not reports:
        raise ArtifactIntegrityError("Report package has no inline XBRL report under reports/")
    if len(reports) > 1:
        raise ArtifactIntegrityError(f"Report package has several reports: {reports}")
    entrypoint = reports[0]
    return tuple(
        PackageMember(
            name=name,
            source_url=url,
            size_bytes=size,
            content_type=mimetypes.guess_type(name)[0],
            checksum_sha256=checksum,
            is_primary_document=name == entrypoint,
            is_xbrl_entrypoint=name == entrypoint,
        )
        for name, checksum, size in sorted(rows)
    )


def _is_report(name: str) -> bool:
    """`<top>/reports/<file>` (or `reports/<file>`) with an (X)HTML suffix."""
    parts = PurePosixPath(name).parts
    return (
        len(parts) in {2, 3}
        and parts[-2].lower() == "reports"
        and PurePosixPath(name).suffix.lower() in _REPORT_SUFFIXES
    )
