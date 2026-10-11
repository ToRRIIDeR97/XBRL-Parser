"""The DART source adapter: Korean periodic reports from the FSS's OpenDART API.

Discovery pages the disclosure search (`/api/list.json`, `pblntf_ty=A` periodic
disclosures) and keeps business (사업보고서), half-year (반기보고서) and quarterly
(분기보고서) reports; each becomes a `FilingRef` keyed `dart-<rcept_no>` with an
entity key allocated from the company's 8-digit corp_code and the report code as
its form. A package is the original XBRL zip (`/api/fnlttXbrl.xml`); its entrypoint
is the single `.xbrl` instance.

The API key (`XBRL_DART_API_KEY`) goes only into the outgoing request's `crtfc_key`
parameter. OpenDART answers errors with a status code in the body: `013` no data,
`020` the key's daily call limit (about 20,000), `010`/`011`/`012`/`901` key or
access problems.
"""

from __future__ import annotations

import calendar
import json
import re
import tempfile
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlencode

from sec_xbrl_warehouse.config import Settings
from sec_xbrl_warehouse.domain.models import (
    Company,
    FilingPackageManifest,
    FilingRef,
    ValidationIssue,
)
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.domain.sources import filing_source, is_filing_key
from sec_xbrl_warehouse.domain.xbrl import XbrlExtractionBundle
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

DART_SOURCE = "dart"
DART_HOST = "opendart.fss.or.kr"
DART_API = f"https://{DART_HOST}/api"
# The entity scheme for DART's 8-digit company code (corp_code).
DART_SCHEME = "https://opendart.fss.or.kr/corp_code"
DART_KEY_PARAM = "crtfc_key"

# opendart.fss.or.kr only; at most 1 request/s, one at a time. `dart_policy` adds
# the warehouse user agent.
DART_POLICY = SourcePolicy(
    code=DART_SOURCE,
    allowed_hosts=frozenset({DART_HOST}),
    max_requests_per_second=1.0,
    max_concurrency=1,
    headers={"Accept": "application/json, application/zip;q=0.9, */*;q=0.8"},
    secret_params=frozenset({DART_KEY_PARAM}),
)

# Scheme-less taxonomy prefix for the FSS's DART taxonomy. Unverified (no filing was
# fetched); confirm it with the canary's namespaces. No pinned package covers it, so
# with `dart` enabled `taxonomy status` reports it `uncovered` (fail closed). Must
# match `source_required_prefixes.dart` in `config/taxonomy_packages.yml`.
DART_TAXONOMY_PREFIXES: tuple[str, ...] = ("dart.fss.or.kr/",)

# Report codes (reprt_code) and the fiscal period each one covers.
ANNUAL, HALF_YEAR, FIRST_QUARTER, THIRD_QUARTER = "11011", "11012", "11013", "11014"
REPORT_PERIODS = {ANNUAL: "FY", HALF_YEAR: "H1", FIRST_QUARTER: "Q1", THIRD_QUARTER: "Q3"}

_KST = timezone(timedelta(hours=9))
_CORP_CODE = re.compile(r"[0-9]{8}")
_RCEPT_NO = re.compile(r"[0-9]{14}")
_REPORT = re.compile(
    r"^\s*(?P<tags>(?:\[[^\]]*\]\s*)*)(?P<kind>사업보고서|반기보고서|분기보고서)\s*"
    r"\((?P<year>[0-9]{4})\.(?P<month>[0-9]{2})\)\s*$"
)
_KINDS = {"사업보고서": ANNUAL, "반기보고서": HALF_YEAR}
_QUARTERS = {"03": FIRST_QUARTER, "09": THIRD_QUARTER}
# Remark (`rm`) flag for a withdrawn filing (철회).
_WITHDRAWN = "철"


def dart_policy(settings: Settings) -> SourcePolicy:
    """`DART_POLICY` with the warehouse user agent."""
    return replace(
        DART_POLICY, headers={**DART_POLICY.headers, "User-Agent": settings.sec_user_agent}
    )


def report_code(report_name: str) -> tuple[str, date, bool] | None:
    """`(reprt_code, period end, is_amendment)` for a periodic report name, else None.

    `사업보고서 (2023.12)` is 11011, `반기보고서` 11012, and `분기보고서` 11013 for a
    March period or 11014 for September (other quarter months have no code to infer
    without the fiscal year end, so they return None). A leading `[기재정정]` or
    `[첨부정정]` tag marks a correction.
    """
    match = _REPORT.match(report_name)
    if match is None:
        return None
    year, month = int(match["year"]), match["month"]
    if not 1 <= int(month) <= 12:
        return None
    code = _KINDS.get(match["kind"]) or _QUARTERS.get(month)
    if code is None:
        return None
    end = date(year, int(month), calendar.monthrange(year, int(month))[1])
    return code, end, "정정" in match["tags"]


class DartSourceAdapter:
    """Discovery, package download, and fiscal focus for OpenDART.

    `http` is the source's `SourceHttpClient` (None on parse-only workers, which only
    use `fiscal_focus`); `entity_keys` allocates entity keys (the control repository).
    """

    code = DART_SOURCE
    taxonomy_prefixes = DART_TAXONOMY_PREFIXES

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
        """Periodic reports received in `[filed_from, filed_to]`, every result page.

        Without a corp_code OpenDART limits the window to three months and refuses
        longer ones with an error status. Status `013` (no data) is an empty result;
        `020` (daily limit) raises `SourceRateLimited`; any other non-`000` status
        raises `SourceAccessError`.
        """
        key = required_key(self.settings.dart_api_key, "DART", "XBRL_DART_API_KEY")
        if request.filed_from is None or request.filed_to is None:
            raise ValueError("DART discovery needs filed_from and filed_to")
        if request.filed_from > request.filed_to:
            raise ValueError("DART discovery needs filed_from on or before filed_to")
        entity: str | None = None
        if request.entity is not None:
            entity = request.entity.strip()
            if _CORP_CODE.fullmatch(entity) is None:
                raise ValueError(f"DART entity must be an 8-digit corp_code: {request.entity!r}")
        forms = frozenset(request.forms)
        http = self._require_http()
        filings: dict[str, FilingRef] = {}
        companies: dict[int, Company] = {}
        keys: dict[str, int] = {}
        number, total = 1, 1
        while number <= total:
            params = {
                DART_KEY_PARAM: key,
                "bgn_de": request.filed_from.strftime("%Y%m%d"),
                "end_de": request.filed_to.strftime("%Y%m%d"),
                "pblntf_ty": "A",
                "page_no": str(number),
                "page_count": str(self.settings.dart_page_count),
            }
            if entity is not None:
                params["corp_code"] = entity
            page = await redacted(
                http.get_json(f"{DART_API}/list.json?{urlencode(params)}"), key, DART_POLICY
            )
            status = str(page.get("status"))
            if status == "013":
                break
            if status != "000":
                _raise_status(status, page.get("message"), "disclosure search")
            items = page.get("list")
            if not isinstance(items, list):
                raise SchemaDriftError("DART disclosure search response has no list")
            try:
                total = int(page.get("total_page") or 1)
            except (TypeError, ValueError):
                raise SchemaDriftError("DART disclosure search has an invalid total_page") from None
            for item in items:
                if not isinstance(item, dict):
                    continue
                if entity is not None and item.get("corp_code") != entity:
                    continue
                try:
                    filing = await self._filing(item, keys, forms)
                except _Skip as skipped:
                    logger.info(
                        "dart_report_skipped", rcept_no=item.get("rcept_no"), reason=str(skipped)
                    )
                    continue
                if filing is None or filing.accession in filings:
                    continue
                filings[filing.accession] = filing
                companies.setdefault(filing.cik, Company(cik=filing.cik, name=filing.company_name))
            number += 1
        return DiscoveryResult(filings=tuple(filings.values()), companies=tuple(companies.values()))

    async def _filing(
        self, item: dict[str, Any], keys: dict[str, int], forms: frozenset[str]
    ) -> FilingRef | None:
        """The item as a `FilingRef`; None for other reports or forms not requested."""
        name = item.get("report_nm")
        parsed = report_code(name) if isinstance(name, str) else None
        if parsed is None or (forms and parsed[0] not in forms):
            return None
        if _WITHDRAWN in str(item.get("rm") or ""):
            raise _Skip("withdrawn report")
        code, period_end, amended = parsed
        rcept_no = item.get("rcept_no")
        if not isinstance(rcept_no, str) or _RCEPT_NO.fullmatch(rcept_no) is None:
            raise _Skip("missing or malformed rcept_no")
        accession = f"dart-{rcept_no}"
        if not is_filing_key(accession):
            raise _Skip("rcept_no is not a valid filing key")
        corp_code = item.get("corp_code")
        if not isinstance(corp_code, str) or _CORP_CODE.fullmatch(corp_code) is None:
            raise _Skip("missing or malformed corp_code")
        try:
            filed = datetime.strptime(str(item.get("rcept_dt")), "%Y%m%d").date()
        except ValueError:
            raise _Skip("invalid rcept_dt") from None
        if corp_code not in keys:
            keys[corp_code] = await self.entity_keys.resolve_entity_key(DART_SCHEME, corp_code)
        company = item.get("corp_name")
        query = urlencode({"rcept_no": rcept_no, "reprt_code": code})
        try:
            return FilingRef(
                accession=accession,
                cik=keys[corp_code],
                company_name=(
                    company.strip() if isinstance(company, str) and company.strip() else corp_code
                ),
                form=code,
                filed_date=filed,
                report_date=period_end,
                archive_directory_url=f"{DART_API}/fnlttXbrl.xml?{query}",
                is_amendment=amended,
                is_inline_xbrl=False,
            )
        except ValueError as exc:
            raise _Skip(f"invalid filing reference: {exc}") from None

    # Package download ------------------------------------------------------------

    async def download_package(self, filing: FilingRef) -> FilingPackageManifest:
        """Download the original XBRL zip for the report, validate it, store it once.

        A body that isn't a zip is OpenDART's status message: `020` raises
        `SourceRateLimited`, anything else `SourceAccessError`; nothing is stored.
        """
        key = required_key(self.settings.dart_api_key, "DART", "XBRL_DART_API_KEY")
        if filing_source(filing.accession) != DART_SOURCE:
            raise UnsupportedSourceError(
                f"The DART adapter can't fetch {filing.source} filing {filing.accession}"
            )
        rcept_no = filing.accession.removeprefix("dart-")
        if _RCEPT_NO.fullmatch(rcept_no) is None:
            raise ValueError(f"Not a DART receipt number: {rcept_no!r}")
        if filing.form not in REPORT_PERIODS:
            raise ValueError(f"Not a DART periodic report code: {filing.form!r}")
        http = self._require_http()
        query = {"rcept_no": rcept_no, "reprt_code": filing.form}
        shown = f"{DART_API}/fnlttXbrl.xml?{urlencode(query)}"
        url = f"{DART_API}/fnlttXbrl.xml?{urlencode({DART_KEY_PARAM: key, **query})}"
        self.settings.ensure_directories()
        with tempfile.TemporaryDirectory(
            prefix="dart-download-", dir=self.settings.temp_dir
        ) as directory:
            archive = Path(directory) / "package.zip"
            await redacted(
                http.download(url, archive, max_bytes=self.settings.filing_max_bytes),
                key,
                DART_POLICY,
            )
            if not zipfile.is_zipfile(archive):
                _raise_body_error(read_error_body(archive), filing.accession)
            return await store_zip_package_async(
                self.settings,
                self.object_store,
                filing,
                archive,
                source=DART_SOURCE,
                source_url=shown,
                entrypoint=_instance,
            )

    # Fiscal focus ----------------------------------------------------------------

    def fiscal_focus(
        self, bundle: XbrlExtractionBundle
    ) -> tuple[XbrlExtractionBundle, Sequence[ValidationIssue]]:
        """The report code's period (11011 `FY`, 11012 `H1`, 11013 `Q1`, 11014 `Q3`)
        and the report period's end year."""
        filing = bundle.filing
        period = REPORT_PERIODS.get(filing.form)
        if period is None:
            return bundle, ()
        updated = filing.model_copy(
            update={
                "fiscal_period": period,
                "fiscal_year": filing.report_date.year if filing.report_date else None,
            }
        )
        return bundle.model_copy(update={"filing": updated}), ()

    def _require_http(self) -> Any:
        if self.http is None:
            raise ConfigurationError(
                "This worker has no DART client; run DART discovery and fetch jobs on a "
                "worker that claims them with dart enabled"
            )
        return self.http


class _Skip(Exception):
    """A DART periodic report that can't become a safe `FilingRef`."""


def _next_reset() -> datetime:
    """Shortly after the next midnight in Korea, when the daily call count resets."""
    tomorrow = datetime.now(_KST).date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(0, 5), tzinfo=_KST).astimezone(UTC)


def _raise_status(status: str, message: object, what: str) -> None:
    detail = DART_POLICY.redact(str(message or ""))[:200]
    if status == "020":
        raise SourceRateLimited(
            f"OpenDART daily call limit reached (status 020) during the {what}",
            retry_at=_next_reset(),
            source=DART_SOURCE,
        )
    raise SourceAccessError(f"OpenDART answered status {status} for the {what}: {detail}")


_XML_STATUS = re.compile(rb"<status>\s*([0-9]{3})\s*</status>")
_XML_MESSAGE = re.compile(rb"<message>(.{0,400}?)</message>", re.DOTALL)


def _raise_body_error(body: bytes, accession: str) -> None:
    """A download that returned OpenDART's status message (XML or JSON), not a zip."""
    status: str | None = None
    message: object = ""
    match = _XML_STATUS.search(body)
    if match is not None:
        status = match.group(1).decode()
        found = _XML_MESSAGE.search(body)
        message = found.group(1).decode("utf-8", "replace") if found else ""
    else:
        try:
            page = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            page = None
        if isinstance(page, dict) and page.get("status") is not None:
            status, message = str(page["status"]), page.get("message")
    if status is None:
        raise SourceAccessError(f"OpenDART returned no zip for {accession}")
    _raise_status(status, message, f"XBRL download of {accession}")


def _instance(names: Sequence[str]) -> str:
    candidates = [name for name in names if PurePosixPath(name).suffix.lower() == ".xbrl"]
    return single_entrypoint(candidates, ".xbrl instance")
