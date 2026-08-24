from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import ValidationIssue
from sec_xbrl_warehouse.exceptions import XbrlExtractionError

_SEVERITY_BY_LEVEL = {
    "debug": Severity.INFO,
    "info": Severity.INFO,
    "warning": Severity.WARNING,
    "error": Severity.ERROR,
    "critical": Severity.FATAL,
    "fatal": Severity.FATAL,
}

_CODE_REJECT_SEVERITIES: dict[str, Severity] = {}


class ArelleIssueNormalizer:
    """Normalize Arelle logger records into domain `ValidationIssue` objects.

    Original message codes and source locations are retained: `code` keeps the
    Arelle code verbatim, `details` keeps the recorded structure (args, file,
    line, column) plus a truncated original message when truncation occurs.
    """

    def __init__(
        self,
        *,
        retain_all_messages: bool = True,
        reject_severities: Sequence[Severity] = (Severity.FATAL,),
        max_message_chars: int = 16_000,
        max_details_chars: int = 2_000,
    ) -> None:
        self.retain_all_messages = retain_all_messages
        self.reject_severities = frozenset(reject_severities)
        self.max_message_chars = max_message_chars
        self.max_details_chars = max_details_chars

    def normalize(
        self,
        records: Iterable[Mapping[str, Any]] | Iterable[Any],
        *,
        accession: str | None = None,
        source: str = "arelle",
    ) -> tuple[ValidationIssue, ...]:
        issues: list[ValidationIssue] = []
        for record in records:
            issue = self.normalize_one(record, accession=accession, source=source)
            if issue is not None:
                issues.append(issue)
        return tuple(issues)

    def normalize_one(
        self,
        record: Mapping[str, Any] | Any,
        *,
        accession: str | None = None,
        source: str = "arelle",
    ) -> ValidationIssue | None:
        if isinstance(record, ValidationIssue):
            return record
        if isinstance(record, Mapping):
            return self._from_mapping(record, accession=accession, source=source)
        return None

    def _from_mapping(
        self,
        record: Mapping[str, Any],
        *,
        accession: str | None,
        source: str,
    ) -> ValidationIssue | None:
        message = _stringify(record.get("message") or record.get("msg") or "")
        level = _stringify(record.get("level") or "warning").lower()
        severity = _SEVERITY_BY_LEVEL.get(level, Severity.WARNING)
        code = _stringify(record.get("code") or "ARELLE_UNCLASSIFIED")
        details = _clean_details(record.get("details"), limit=self.max_details_chars)
        if not self.retain_all_messages and severity not in self.reject_severities:
            return None
        if len(message) > self.max_message_chars:
            details = {**details, "truncated_message": message[: self.max_message_chars]}
            message = message[: self.max_message_chars]
        return ValidationIssue(
            source=source,
            code=code,
            severity=severity,
            message=message,
            accession=accession,
            source_document=_optional_str(record.get("file") or record.get("source_document")),
            line=_optional_int(record.get("line")),
            column=_optional_int(record.get("column") or record.get("column_number")),
            details=details,
        )


def reject_fatal_issues(issues: Iterable[ValidationIssue]) -> None:
    """Raise when any FATAL issue is present regardless of configuration."""
    fatal = [issue for issue in issues if issue.severity is Severity.FATAL]
    if fatal:
        codes = sorted({item.code for item in fatal})
        raise XbrlExtractionError(f"Arelle reported fatal validation issues: {codes}")


def reject_configured_issues(
    issues: Iterable[ValidationIssue],
    reject_severities: Sequence[Severity],
) -> tuple[ValidationIssue, ...]:
    """Return issues at or above a rejection severity threshold."""
    blocked = frozenset(reject_severities)
    return tuple(issue for issue in issues if issue.severity in blocked)


def summarize_issues(issues: Iterable[ValidationIssue]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for issue in issues:
        key = f"{issue.severity.value}:{issue.code}"
        summary[key] = summary.get(key, 0) + 1
    return dict(sorted(summary.items()))


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, dict)):
        # Arelle log records may carry structured args here.
        return str(value)
    return str(value)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean_details(value: Any, *, limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)) or item is None:
            cleaned[str(key)] = item
        else:
            cleaned[str(key)] = str(item)[:limit]
    return cleaned
