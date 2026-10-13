"""The EDINET source adapter: Japanese securities reports from the FSA's EDINET API v2.

Discovery reads the document list (`/api/v2/documents.json?type=2`) one day at a
time and keeps periodic reports that carry XBRL; each becomes a `FilingRef` keyed
`edinet-<docID>` with an entity key allocated from the filer's EDINET code. A
package is the "submitted document and audit report" zip (`type=1`); its entrypoint
is the single instance under `XBRL/PublicDoc/`.

The API key (`XBRL_EDINET_API_KEY`) goes only into the outgoing request's
`Subscription-Key` parameter. EDINET answers errors with HTTP 200 and a JSON body,
so every response is checked for its status before use.
"""

from __future__ import annotations

import json
import re
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import (
    Company,
    FilingPackageManifest,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.sources import filing_source, is_filing_key
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle, XbrlFact
from sec_xbrl_warehouse.exceptions import (
    ConfigurationError,
    SchemaDriftError,
    SourceAccessError,
    SourceRateLimited,
    UnsupportedSourceError,
)
from sec_xbrl_warehouse.logging import get_logger
from sec_xbrl_warehouse.sources.base import DiscoveryRequest, DiscoveryResult, SourcePolicy
from sec_xbrl_warehouse.sources.keyed import (
    EntityKeys,
    read_error_body,
    redacted,
    required_key,
    single_entrypoint,
    store_zip_package_async,
)

logger = get_logger(__name__)

EDINET_SOURCE = "edinet"
EDINET_HOST = "api.edinet-fsa.go.jp"
EDINET_API = f"https://{EDINET_HOST}/api/v2"
# The entity identifier scheme EDINET instances use with the EDINET code.
EDINET_SCHEME = "http://disclosure.edinet-fsa.go.jp"
EDINET_KEY_PARAM = "Subscription-Key"

# api.edinet-fsa.go.jp only; at most 1 request/s, one at a time. `edinet_policy`
# adds the warehouse user agent.
EDINET_POLICY = SourcePolicy(
    code=EDINET_SOURCE,
    allowed_hosts=frozenset({EDINET_HOST}),
    max_requests_per_second=1.0,
    max_concurrency=1,
    headers={"Accept": "application/json, application/octet-stream;q=0.9, */*;q=0.8"},
    secret_params=frozenset({EDINET_KEY_PARAM}),
)

# Scheme-less taxonomy prefixes EDINET filings import: the FSA's EDINET taxonomy
# (jpdei, jpcrp, jppfs, jpigp, ...). No pinned package covers it yet, so with
# `edinet` enabled `taxonomy status` reports it `uncovered` (fail closed). Must match
# `source_required_prefixes.edinet` in `config/taxonomy_packages.yml`.
EDINET_TAXONOMY_PREFIXES: tuple[str, ...] = ("disclosure.edinet-fsa.go.jp/taxonomy/",)

# Document type codes (EDINET API specification v2, 4-1): 120 annual securities
# report, 130 its amendment, 140/150 quarterly, 160/170 semi-annual.
AMENDMENT_TYPES = frozenset({"130", "150", "170"})
ANNUAL_TYPES = frozenset({"120", "130"})

_DOC_ID = re.compile(r"[A-Z0-9]{8}")
_EDINET_CODE = re.compile(r"[A-Z][0-9]{5}")
_JPDEI_NAMESPACE = re.compile(
    r"^http://disclosure\.edinet-fsa\.go\.jp/taxonomy/jpdei/\d{4}-\d{2}-\d{2}/jpdei_cor$"
)
_FISCAL_YEAR_END = "CurrentFiscalYearEndDateDEI"
_PERIOD_TYPE = "TypeOfCurrentPeriodDEI"
_PERIODS = {"FY": "FY", "HY": "H1", "Q1": "Q1", "Q2": "Q2", "Q3": "Q3"}
# EDINET publishes no fixed window for its 429 answer; try again after this delay.
_RATE_LIMIT_DELAY = timedelta(minutes=15)


def edinet_policy(settings: Settings) -> SourcePolicy:
    """`EDINET_POLICY` with the warehouse user agent."""
    return replace(
        EDINET_POLICY, headers={**EDINET_POLICY.headers, "User-Agent": settings.sec_user_agent}
    )


def normalize_edinet_code(value: object) -> str | None:
    """An EDINET code (`E02144`) trimmed and upper-cased; None if it isn't one."""
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if _EDINET_CODE.fullmatch(code) else None


class EdinetSourceAdapter:
    """Discovery, package download, and fiscal focus for EDINET.

    `http` is the source's `SourceHttpClient` (None on parse-only workers, which only
    use `fiscal_focus`); `entity_keys` allocates entity keys (the control repository).
    """

    code = EDINET_SOURCE
    taxonomy_prefixes = EDINET_TAXONOMY_PREFIXES

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
        """Periodic reports with XBRL listed on each day of `[filed_from, filed_to]`.

        Both bounds are required and the window may span at most `edinet_max_days`
        days. One list request per day, oldest first. Kept: `xbrlFlag` 1, not
        withdrawn, disclosed, a document type in `request.forms` (or the configured
        `edinet_doc_types`), and the requested EDINET code when one is given.
        """
        key = required_key(self.settings.edinet_api_key, "EDINET", "XBRL_EDINET_API_KEY")
        if request.filed_from is None or request.filed_to is None:
            raise ValueError("EDINET discovery needs filed_from and filed_to")
        if request.filed_from > request.filed_to:
            raise ValueError("EDINET discovery needs filed_from on or before filed_to")
        days = (request.filed_to - request.filed_from).days + 1
        if days > self.settings.edinet_max_days:
            raise ValueError(
                f"EDINET discovery spans {days} days; the limit is "
                f"{self.settings.edinet_max_days} (XBRL_EDINET_MAX_DAYS)"
            )
        entity: str | None = None
        if request.entity is not None:
            entity = normalize_edinet_code(request.entity)
            if entity is None:
                raise ValueError(f"EDINET entity must be an EDINET code: {request.entity!r}")
        forms = frozenset(request.forms or self.settings.edinet_doc_types)
        http = self._require_http()
        filings: dict[str, FilingRef] = {}
        companies: dict[int, Company] = {}
        keys: dict[str, int] = {}
        for offset in range(days):
            day = request.filed_from + timedelta(days=offset)
            params = {"date": day.isoformat(), "type": "2", EDINET_KEY_PARAM: key}
            page = await redacted(
                http.get_json(f"{EDINET_API}/documents.json?{urlencode(params)}"),
                key,
                EDINET_POLICY,
            )
            _check_status(page, f"document list for {day.isoformat()}")
            results = page.get("results")
            if not isinstance(results, list):
                raise SchemaDriftError(f"EDINET document list for {day} has no results list")
            for record in results:
                if not isinstance(record, dict) or not _wanted(record, forms, entity):
                    continue
                try:
                    filing = await self._filing(record, keys)
                except _Skip as skipped:
                    logger.warning(
                        "edinet_record_skipped", doc_id=record.get("docID"), reason=str(skipped)
                    )
                    continue
                if filing.accession in filings:
                    continue
                filings[filing.accession] = filing
                companies.setdefault(filing.cik, Company(cik=filing.cik, name=filing.company_name))
        return DiscoveryResult(filings=tuple(filings.values()), companies=tuple(companies.values()))

    async def _filing(self, record: dict[str, Any], keys: dict[str, int]) -> FilingRef:
        doc_id = record.get("docID")
        if not isinstance(doc_id, str) or _DOC_ID.fullmatch(doc_id) is None:
            raise _Skip("missing or malformed docID")
        accession = f"edinet-{doc_id}"
        if not is_filing_key(accession):
            raise _Skip("docID is not a valid filing key")
        code = normalize_edinet_code(record.get("edinetCode"))
        if code is None:
            raise _Skip("missing or malformed edinetCode")
        try:
            filed = _date(record.get("submitDateTime"))
            report_date = _date(record.get("periodEnd"))
        except ValueError:
            raise _Skip("invalid submitDateTime or periodEnd") from None
        if filed is None:
            raise _Skip("missing submitDateTime")
        if code not in keys:
            keys[code] = await self.entity_keys.resolve_entity_key(EDINET_SCHEME, code)
        name = record.get("filerName")
        doc_type = str(record.get("docTypeCode"))
        try:
            return FilingRef(
                accession=accession,
                cik=keys[code],
                company_name=name.strip() if isinstance(name, str) and name.strip() else code,
                form=doc_type,
                filed_date=filed,
                report_date=report_date,
                archive_directory_url=f"{EDINET_API}/documents/{doc_id}",
                is_amendment=doc_type in AMENDMENT_TYPES,
                is_inline_xbrl=False,
            )
        except ValueError as exc:
            raise _Skip(f"invalid filing reference: {exc}") from None

    # Package download ------------------------------------------------------------

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        """Download the `type=1` zip, validate it, and store it once.

        A JSON (error) body instead of a zip is a `SourceAccessError` (a 429 answer is
        `SourceRateLimited`) and nothing is stored. The entrypoint is the single
        `XBRL/PublicDoc/*.xbrl` instance; the audit report's instance is a plain member.
        """
        key = required_key(self.settings.edinet_api_key, "EDINET", "XBRL_EDINET_API_KEY")
        if filing_source(filing.accession) != EDINET_SOURCE:
            raise UnsupportedSourceError(
                f"The EDINET adapter can't fetch {filing.source} filing {filing.accession}"
            )
        doc_id = filing.accession.removeprefix("edinet-")
        if _DOC_ID.fullmatch(doc_id) is None:
            raise ValueError(f"Not an EDINET document ID: {doc_id!r}")
        http = self._require_http()
        shown = f"{EDINET_API}/documents/{doc_id}?type=1"
        url = f"{shown}&{urlencode({EDINET_KEY_PARAM: key})}"
        self.settings.ensure_directories()
        with tempfile.TemporaryDirectory(
            prefix="edinet-download-", dir=self.settings.temp_dir
        ) as directory:
            archive = Path(directory) / "package.zip"
            await redacted(
                http.download(url, archive, max_bytes=self.settings.filing_max_bytes),
                key,
                EDINET_POLICY,
            )
            if not zipfile.is_zipfile(archive):
                _raise_body_error(read_error_body(archive), filing.accession)
            return await store_zip_package_async(
                self.settings,
                self.object_store,
                filing,
                archive,
                source=EDINET_SOURCE,
                source_url=shown,
                entrypoint=_public_instance,
            )

    # Fiscal focus ----------------------------------------------------------------

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        """Fiscal fields from the jpdei cover facts, with a document-type fallback.

        `CurrentFiscalYearEndDateDEI` gives the year and `TypeOfCurrentPeriodDEI` the
        period (`FY`, `Q1`-`Q3`, `HY` as `H1`). Without them an annual report (120,
        130) is `FY` of its period-end year; other filings keep their values.
        """
        filing = bundle.filing
        issues: list[ValidationIssue] = []
        year_end = _single(bundle.facts, _FISCAL_YEAR_END, filing, issues)
        period_type = _single(bundle.facts, _PERIOD_TYPE, filing, issues)
        updates: dict[str, Any] = {}
        if year_end is not None:
            try:
                updates["fiscal_year"] = date.fromisoformat(year_end[:10]).year
            except ValueError:
                issues.append(_issue(filing, f"Invalid {_FISCAL_YEAR_END}: {year_end!r}"))
        if period_type is not None:
            period = _PERIODS.get(period_type.upper())
            if period is None:
                issues.append(_issue(filing, f"Unknown {_PERIOD_TYPE}: {period_type!r}"))
            else:
                updates["fiscal_period"] = period
        if filing.form in ANNUAL_TYPES:
            updates.setdefault("fiscal_period", "FY")
            if "fiscal_year" not in updates and filing.report_date is not None:
                updates["fiscal_year"] = filing.report_date.year
        if not updates:
            return bundle, tuple(issues)
        updated = filing.model_copy(update=updates)
        return bundle.model_copy(update={"filing": updated}), tuple(issues)

    def _require_http(self) -> Any:
        if self.http is None:
            raise ConfigurationError(
                "This worker has no EDINET client; run EDINET discovery and fetch jobs on "
                "a worker that claims them with edinet enabled"
            )
        return self.http


class _Skip(Exception):
    """An EDINET list record that can't become a safe `FilingRef`."""


def _wanted(record: dict[str, Any], forms: frozenset[str], entity: str | None) -> bool:
    """XBRL periodic reports that are neither withdrawn nor hidden, in the request."""
    if str(record.get("xbrlFlag")) != "1" or str(record.get("withdrawalStatus")) != "0":
        return False
    # "1"/"2": the FSA hid the document (it can't be fetched); "1" on docInfoEditStatus
    # is a later notice that the FSA corrected an earlier document's metadata.
    if str(record.get("disclosureStatus", "0")) in {"1", "2"}:
        return False
    if str(record.get("docInfoEditStatus", "0")) == "1":
        return False
    if str(record.get("docTypeCode")) not in forms:
        return False
    return entity is None or normalize_edinet_code(record.get("edinetCode")) == entity


def _check_status(page: dict[str, Any], what: str) -> None:
    """Raise for an EDINET error body (`metadata.status` or a top-level `StatusCode`)."""
    status, message = _status(page)
    if status == "200":
        return
    _raise_status(status, message, what)


def _status(page: dict[str, Any]) -> tuple[str, str]:
    if "StatusCode" in page:
        return str(page.get("StatusCode")), str(page.get("message") or "")
    metadata = page.get("metadata")
    if not isinstance(metadata, dict):
        raise SchemaDriftError("EDINET response has no metadata")
    return str(metadata.get("status")), str(metadata.get("message") or "")


def _raise_status(status: str, message: str, what: str) -> None:
    detail = EDINET_POLICY.redact(message)[:200]
    if status == "429":
        raise SourceRateLimited(
            f"EDINET answered 429 Too Many Requests for the {what}",
            retry_at=datetime.now(UTC) + _RATE_LIMIT_DELAY,
            source=EDINET_SOURCE,
        )
    raise SourceAccessError(f"EDINET answered status {status} for the {what}: {detail}")


def _raise_body_error(body: bytes, accession: str) -> None:
    """A download that returned a JSON error (or anything else) instead of a zip."""
    try:
        page = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SourceAccessError(f"EDINET returned no zip for {accession}") from None
    if not isinstance(page, dict):
        raise SourceAccessError(f"EDINET returned no zip for {accession}")
    try:
        status, message = _status(page)
    except SchemaDriftError:
        raise SourceAccessError(f"EDINET returned no zip for {accession}") from None
    _raise_status(status, message, f"package of {accession}")
    raise SourceAccessError(f"EDINET returned no zip for {accession} (status {status})")


def _public_instance(names: Sequence[str]) -> str:
    """The single `XBRL/PublicDoc/*.xbrl` instance (the audit report's is under AuditDoc)."""
    candidates = [
        name
        for name in names
        if PurePosixPath(name).parent == PurePosixPath("XBRL/PublicDoc")
        and PurePosixPath(name).suffix.lower() == ".xbrl"
    ]
    return single_entrypoint(candidates, "XBRL/PublicDoc instance")


def _date(value: object) -> date | None:
    """`YYYY-MM-DD` from a date or `YYYY-MM-DD hh:mm` string; None when absent."""
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"Not a date: {value!r}")
    return date.fromisoformat(value.strip()[:10])


def _single(
    facts: Sequence[XbrlFact], name: str, filing: FilingRef, issues: list[ValidationIssue]
) -> str | None:
    """The one non-dimensional jpdei value of `name`; None if absent or conflicting."""
    values = {
        _lexical(fact)
        for fact in facts
        if fact.concept_name == name
        and not fact.dimensions
        and not fact.is_nil
        and _JPDEI_NAMESPACE.match(fact.concept_namespace)
    }
    values.discard("")
    if len(values) > 1:
        issues.append(_issue(filing, f"Conflicting {name} values: {sorted(values)}"))
        return None
    return next(iter(values), None)


def _lexical(fact: XbrlFact) -> str:
    if fact.value_date is not None:
        return fact.value_date.isoformat()
    return (fact.value_text if fact.value_text is not None else fact.raw_value or "").strip()


def _issue(filing: FilingRef, message: str) -> ValidationIssue:
    return ValidationIssue(
        source="fiscal_focus",
        code="warehouse:fiscalFocusInvalid",
        severity=Severity.WARNING,
        accession=filing.accession,
        message=message,
    )
