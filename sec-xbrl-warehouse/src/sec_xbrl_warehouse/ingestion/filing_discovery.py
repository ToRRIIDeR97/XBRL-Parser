from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sec_xbrl_warehouse.domain.models import Company, FilingRef, FormerName
from sec_xbrl_warehouse.domain.ports import ObjectStore
from sec_xbrl_warehouse.exceptions import SecAccessError
from sec_xbrl_warehouse.infrastructure.http.sec_client import (
    SecHttpClient,
    build_archive_directory_url,
)
from sec_xbrl_warehouse.infrastructure.storage.hashing import sha256_bytes, stable_json_hash


class SecSubmissionsDiscovery:
    """Discover issuer filings from `data.sec.gov/submissions` JSON."""

    def __init__(self, http: SecHttpClient, data_api_base: str = "https://data.sec.gov") -> None:
        self.http = http
        self.data_api_base = data_api_base.rstrip("/")

    async def discover_company_filings(
        self,
        cik: int,
        *,
        forms: Sequence[str],
        filed_from: date | None = None,
        filed_to: date | None = None,
        include_older_files: bool = True,
    ) -> tuple[FilingRef, ...]:
        """Named company-discovery entry point used by the handoff."""
        return await self.discover(
            cik,
            forms=forms,
            filed_from=filed_from,
            filed_to=filed_to,
            include_older_files=include_older_files,
        )

    async def discover(
        self,
        cik: int,
        *,
        forms: Sequence[str],
        filed_from: date | None = None,
        filed_to: date | None = None,
        include_older_files: bool = True,
    ) -> tuple[FilingRef, ...]:
        """Fetch and filter current and date-relevant archived submissions for one CIK."""
        _, filings = await self.discover_with_company(
            cik,
            forms=forms,
            filed_from=filed_from,
            filed_to=filed_to,
            include_older_files=include_older_files,
        )
        return filings

    async def discover_with_company(
        self,
        cik: int,
        *,
        forms: Sequence[str],
        filed_from: date | None = None,
        filed_to: date | None = None,
        include_older_files: bool = True,
    ) -> tuple[Company, tuple[FilingRef, ...]]:
        """`discover`, plus the company parsed from the same submissions document."""
        url = f"{self.data_api_base}/submissions/CIK{cik:010d}.json"
        payload = await self.http.get_json(url)
        company = parse_company(payload, fetched_at=datetime.now(UTC))
        company_name = str(payload.get("name") or f"CIK {cik}")
        results = list(
            parse_submissions_recent(
                payload,
                cik=cik,
                company_name=company_name,
                forms=set(forms),
                filed_from=filed_from,
                filed_to=filed_to,
            )
        )
        if include_older_files:
            for descriptor in payload.get("filings", {}).get("files", []):
                # Use the descriptor's date range to skip archived files outside
                # the requested range before any network access.
                descriptor_from = _optional_date(descriptor.get("filingFrom"))
                descriptor_to = _optional_date(descriptor.get("filingTo"))
                if (
                    filed_from is not None
                    and descriptor_to is not None
                    and descriptor_to < filed_from
                ):
                    continue
                if (
                    filed_to is not None
                    and descriptor_from is not None
                    and descriptor_from > filed_to
                ):
                    continue
                name = descriptor.get("name")
                if not name:
                    continue
                older = await self.http.get_json(f"{self.data_api_base}/submissions/{name}")
                results.extend(
                    parse_columnar_filing_block(
                        older,
                        cik=cik,
                        company_name=company_name,
                        forms=set(forms),
                        filed_from=filed_from,
                        filed_to=filed_to,
                    )
                )
        unique = {item.accession: item for item in results}
        return company, tuple(
            sorted(unique.values(), key=lambda filing: (filing.filed_date, filing.accession))
        )


def parse_company(payload: dict[str, Any], *, fetched_at: datetime | None = None) -> Company:
    """Map a submissions JSON document to `Company`.

    Missing or malformed optional fields become `None` or empty tuples. Contact
    data (EIN, phone, addresses) is not kept.
    """
    cik = int(str(payload.get("cik") or "").strip())
    return Company(
        cik=cik,
        name=str(payload.get("name") or f"CIK {cik}").strip(),
        entity_type=_optional_text(payload.get("entityType")),
        sic=_optional_int(payload.get("sic")),
        sic_description=_optional_text(payload.get("sicDescription")),
        **_tickers_and_exchanges(payload.get("tickers"), payload.get("exchanges")),
        fiscal_year_end=_fiscal_year_end(payload.get("fiscalYearEnd")),
        state_of_incorporation=_optional_text(payload.get("stateOfIncorporation")),
        filer_category=_optional_text(payload.get("category")),
        former_names=tuple(
            FormerName(
                name=str(item["name"]).strip(),
                valid_from=_timestamp_date(item.get("from")),
                valid_to=_timestamp_date(item.get("to")),
            )
            for item in payload.get("formerNames") or ()
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ),
        source_checksum=stable_json_hash(payload),
        fetched_at=fetched_at,
    )


async def enrich_companies(
    control: Any,
    http: Any,
    *,
    ciks: Iterable[int],
    as_of: date,
    data_api_base: str = "https://data.sec.gov",
    max_age_days: int | None = None,
) -> int:
    """Fetch submissions for new (and, optionally, stale) CIKs and store them.

    The input is deduplicated. CIKs without a company row are always fetched.
    With `max_age_days`, stored companies fetched more than that many days before
    `as_of` are fetched again. Each discovery run costs at most one rate-limited
    request per such CIK. Returns the number of companies stored.
    """
    wanted = sorted({int(cik) for cik in ciks})
    if not wanted:
        return 0
    skip = set(await control.known_company_ciks(wanted))
    if max_age_days is not None and skip:
        cutoff = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC) - timedelta(
            days=max_age_days
        )
        skip -= set(await control.stale_company_ciks(sorted(skip), cutoff))
    stored = 0
    for cik in wanted:
        if cik in skip:
            continue
        payload = await http.get_json(f"{data_api_base.rstrip('/')}/submissions/CIK{cik:010d}.json")
        await control.upsert_company(parse_company(payload, fetched_at=datetime.now(UTC)))
        stored += 1
    return stored


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _tickers_and_exchanges(tickers: Any, exchanges: Any) -> dict[str, tuple[str, ...]]:
    """Keep SEC's parallel `tickers`/`exchanges` lists aligned.

    When both lists have the same length, positions with a blank ticker are dropped
    from both and a missing exchange becomes "". Otherwise each list is cleaned
    on its own.
    """
    if isinstance(tickers, list) and isinstance(exchanges, list) and len(tickers) == len(exchanges):
        pairs = [
            (str(ticker).strip(), str(exchange).strip() if exchange is not None else "")
            for ticker, exchange in zip(tickers, exchanges, strict=True)
            if ticker is not None and str(ticker).strip()
        ]
        return {
            "tickers": tuple(ticker for ticker, _ in pairs),
            "exchanges": tuple(exchange for _, exchange in pairs),
        }
    return {"tickers": _text_tuple(tickers), "exchanges": _text_tuple(exchanges)}


def _text_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if item is not None and str(item).strip())


def _fiscal_year_end(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None or len(text) != 4 or not text.isdigit():
        return None
    try:
        date(2000, int(text[:2]), int(text[2:]))  # leap year, so 0229 is valid
    except ValueError:
        return None
    return text


def _timestamp_date(value: Any) -> date | None:
    text = _optional_text(value)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return _optional_date(text[:10])


class SecDailyIndexDiscovery:
    """Discover all newly accepted filings from EDGAR daily indexes."""

    def __init__(
        self,
        http: SecHttpClient,
        index_base: str = "https://www.sec.gov/Archives/edgar/daily-index",
        archive_base: str = "https://www.sec.gov/Archives/edgar/data",
        object_store: ObjectStore | None = None,
    ) -> None:
        self.http = http
        self.index_base = index_base.rstrip("/")
        self.archive_base = archive_base.rstrip("/")
        self.object_store = object_store

    async def discover_date(self, filed_date: date, forms: Sequence[str]) -> tuple[FilingRef, ...]:
        """Discover one date from a checksum-retained EDGAR daily index."""
        year = filed_date.year
        quarter = (filed_date.month - 1) // 3 + 1
        # SEC publishes quarterly directories; the master file lives under QTR{quarter}.
        url = f"{self.index_base}/{year}/QTR{quarter}/master.{filed_date.strftime('%Y%m%d')}.idx"
        text = await self.http.get_text(url)
        if self.object_store is not None:
            data = text.encode("utf-8")
            checksum = sha256_bytes(data)
            await asyncio.to_thread(
                self.object_store.put_bytes,
                data,
                f"bronze/sec/daily-index/{filed_date.isoformat()}/sha256={checksum}/master.idx",
                metadata={"source-url": url},
            )
        return self._parse_master(text, filed_date=filed_date, forms=set(forms))

    async def discover_range(
        self,
        filed_from: date,
        filed_to: date,
        forms: Sequence[str],
    ) -> tuple[FilingRef, ...]:
        """Discover every calendar date in an inclusive range."""
        results: list[FilingRef] = []
        current = filed_from
        while current <= filed_to:
            try:
                results.extend(await self.discover_date(current, forms))
            except SecAccessError as exc:
                if "404" in str(exc) or "not found" in str(exc).lower():
                    # SEC has no index for some non-business dates; not fatal.
                    pass
                else:
                    raise
            current += timedelta(days=1)
        return merge_discovery_results(results)

    @staticmethod
    def _parse_master(text: str, *, filed_date: date, forms: set[str]) -> tuple[FilingRef, ...]:
        """Parse a `master.YYYYMMDD.idx` payload.

        Records are ``CIK|Company Name|Form Type|Date Filed|File Name``. Company
        names may contain the pipe separator in theory, so split from the right:
        the last four pipe-separated fields are deterministic.
        """
        from sec_xbrl_warehouse.infrastructure.http.sec_client import (
            build_archive_directory_url,
        )

        filings: list[FilingRef] = []
        for line in text.splitlines():
            if not line or line.startswith("Description:") or line.startswith("Last Data"):
                continue
            if line.startswith("CIK|") or set(line.strip()) <= {"-"} | {" "}:
                continue
            parts = line.split("|")
            if len(parts) < 5:
                continue
            *name_parts, form, filed_text, file_name = parts[-4:]
            cik_text = parts[0]
            try:
                cik = int(cik_text)
            except ValueError:
                continue
            form_type = form.strip()
            if forms and form_type not in forms:
                continue
            try:
                filed = date.fromisoformat(filed_text.strip())
            except ValueError:
                filed = filed_date
            accession = _accession_from_filename(file_name.strip())
            if accession is None:
                continue
            company_name = "|".join(name_parts).strip()
            archive_url = build_archive_directory_url(cik, accession)
            filings.append(
                FilingRef(
                    accession=accession,
                    cik=cik,
                    company_name=company_name,
                    form=form_type,
                    filed_date=filed,
                    archive_directory_url=archive_url,
                    is_amendment=form_type.endswith("/A"),
                )
            )
        return tuple(filings)

    async def discover(self, filed_date: date, forms: Sequence[str]) -> tuple[FilingRef, ...]:
        """Compatibility alias for `discover_date`."""
        return await self.discover_date(filed_date, forms)


def _accession_from_filename(file_name: str) -> str | None:
    """Extract the dashed accession from an EDGAR file path like ``edgar/data/1000275/0000950103-24-017793.txt``."""
    import re

    match = re.search(r"(\d{10}-\d{2}-\d{6})", file_name)
    if match:
        return match.group(1)
    parts = file_name.strip("/").split("/")
    for part in reversed(parts):
        digits = part.split(".")[0].replace("-", "")
        if len(digits) == 18 and digits.isdigit():
            return f"{digits[:10]}-{digits[10:12]}-{digits[12:18]}"
    return None


def merge_discovery_results(
    *groups: Sequence[FilingRef],
) -> tuple[FilingRef, ...]:
    """Merge sources by accession and reject conflicting immutable metadata.

    Conflicting CIK/form/filed-date on the same accession is an integrity error:
    the accession is the SEC's logical filing identity, so a disagreement means
    one source mis-parsed the record or the accession was reused (never legal for
    EDGAR). The first source that supplies a value wins for non-immutable fields.
    """
    merged: dict[str, FilingRef] = {}
    for group in groups:
        for filing in group:
            existing = merged.get(filing.accession)
            if existing and (
                existing.cik != filing.cik
                or existing.form != filing.form
                or existing.filed_date != filing.filed_date
            ):
                raise ValueError(f"Conflicting discovery metadata for {filing.accession}")
            merged[filing.accession] = filing if existing is None else existing
    return tuple(sorted(merged.values(), key=lambda item: (item.filed_date, item.accession)))


def parse_submissions_recent(
    payload: dict[str, Any],
    *,
    cik: int,
    company_name: str,
    forms: set[str],
    filed_from: date | None,
    filed_to: date | None,
) -> tuple[FilingRef, ...]:
    block = payload.get("filings", {}).get("recent", {})
    return parse_columnar_filing_block(
        block,
        cik=cik,
        company_name=company_name,
        forms=forms,
        filed_from=filed_from,
        filed_to=filed_to,
    )


def parse_columnar_filing_block(
    block: dict[str, Any],
    *,
    cik: int,
    company_name: str,
    forms: set[str],
    filed_from: date | None,
    filed_to: date | None,
) -> tuple[FilingRef, ...]:
    accessions = list(block.get("accessionNumber", []))
    rows: list[FilingRef] = []
    for index, accession in enumerate(accessions):
        form = _at(block, "form", index)
        filed = _parse_date(_at(block, "filingDate", index))
        if not accession or not form or filed is None:
            continue
        if forms and form not in forms:
            continue
        if filed_from and filed < filed_from:
            continue
        if filed_to and filed > filed_to:
            continue
        archive_url = build_archive_directory_url(cik, accession)
        primary = _at(block, "primaryDocument", index)
        rows.append(
            FilingRef(
                accession=accession,
                cik=cik,
                company_name=company_name,
                form=form,
                filed_date=filed,
                accepted_at=_parse_datetime(_at(block, "acceptanceDateTime", index)),
                report_date=_parse_date(_at(block, "reportDate", index)),
                fiscal_year=_optional_int(_at(block, "fiscalYear", index)),
                fiscal_period=_at(block, "fiscalPeriod", index) or None,
                primary_document=primary or None,
                archive_directory_url=archive_url,
                primary_document_url=f"{archive_url}/{primary}" if primary else None,
                is_amendment=form.endswith("/A"),
                is_inline_xbrl=_optional_bool(_at(block, "isInlineXBRL", index)),
            )
        )
    return tuple(rows)


def _at(block: dict[str, Any], key: str, index: int) -> Any:
    values = block.get(key) or []
    return values[index] if index < len(values) else None


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _optional_date(value: Any) -> date | None:
    return _parse_date(value)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        if len(text) == 14 and text.isdigit():
            return datetime.strptime(text, "%Y%m%d%H%M%S")
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> bool | None:
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    return None
