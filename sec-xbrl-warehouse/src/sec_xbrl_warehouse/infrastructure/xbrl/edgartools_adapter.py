from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from sec_xbrl_warehouse.domain.models import FilingRef


class EdgarToolsDiscoveryAdapter:
    """Optional convenience adapter around EdgarTools.

    This adapter must not become the canonical XBRL parser. Its role is developer
    ergonomics: company/ticker lookup, filing discovery, and exploratory access.
    Raw EDGAR URLs/accessions must still be recorded and Arelle remains the
    full-fidelity extraction boundary.
    """

    def discover_company_filings(
        self,
        identifier: str | int,
        *,
        forms: Sequence[str],
        filed_from: date | None = None,
        filed_to: date | None = None,
    ) -> tuple[FilingRef, ...]:
        """Return normalized filing references for a ticker, CIK, or company."""
        company = self._company_lookup(identifier)
        filings: list[FilingRef] = []
        for filing in company.filings():
            form = str(getattr(filing, "form", "") or "")
            if forms and form not in forms:
                continue
            filed = _as_date(getattr(filing, "filing_date", None))
            if filed is None:
                continue
            if filed_from is not None and filed < filed_from:
                continue
            if filed_to is not None and filed > filed_to:
                continue
            accession = _dashed_accession(
                str(getattr(filing, "accession_no", "") or getattr(filing, "accession", "") or "")
            )
            if accession is None:
                continue
            cik = int(getattr(filing, "cik", None) or 0)
            if not cik:
                continue
            primary = str(getattr(filing, "primary_document", "") or "") or None
            instance = str(getattr(filing, "instance_document", "") or "") or None
            archive_url = (
                f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}"
            )
            filings.append(
                FilingRef(
                    accession=accession,
                    cik=cik,
                    company_name=str(getattr(company, "name", "") or f"CIK {cik}"),
                    form=form,
                    filed_date=filed,
                    primary_document=primary,
                    instance_document=instance,
                    archive_directory_url=archive_url,
                    primary_document_url=(f"{archive_url}/{primary}" if primary else None),
                    is_amendment=form.endswith("/A"),
                    is_inline_xbrl=bool(getattr(filing, "is_inline_xbrl", None) or None),
                )
            )
        return tuple(filings)

    @staticmethod
    def _company_lookup(identifier: str | int) -> Any:
        try:
            from edgar import Company
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "EdgarTools is not installed; use SecSubmissionsDiscovery instead"
            ) from exc
        if isinstance(identifier, int):
            return Company(identifier)
        return Company(identifier)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        if hasattr(value, "date"):
            value = value.date()
        from datetime import datetime

        if isinstance(value, datetime):
            return value.date()
        return date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _dashed_accession(value: str) -> str | None:
    """Normalize an accession to the dashed form ``0000000001-24-000001``."""
    import re

    cleaned = value.strip()
    match = re.fullmatch(r"(\d{10})-(\d{2})-(\d{6})", cleaned)
    if match:
        return cleaned
    digits = cleaned.replace("-", "").replace("/", "")
    if len(digits) == 18 and digits.isdigit():
        return f"{digits[:10]}-{digits[10:12]}-{digits[12:18]}"
    return None
