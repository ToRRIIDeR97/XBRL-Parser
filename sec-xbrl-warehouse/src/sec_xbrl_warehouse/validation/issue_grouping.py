"""Collapse repeated filing-extraction diagnostics into one row per group."""

from __future__ import annotations

from collections.abc import Iterable

from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import ValidationIssue

MAX_SAMPLES = 5


def group_issues(
    issues: Iterable[ValidationIssue], *, extraction_revision: int | None = None
) -> tuple[ValidationIssue, ...]:
    """Group issues sharing `(source, code, severity, source_document)`.

    The first occurrence keeps its message, line, column, and details; a group
    of more than one adds `details.occurrences` and up to five
    `details.samples` (line, column, message) in input order. Fatal issues are
    never grouped. Every returned issue is tagged with `extraction_revision`
    when one is given. Output keeps the order of each group's first occurrence.
    """
    groups: dict[object, list[ValidationIssue]] = {}
    for index, issue in enumerate(issues):
        key: object = (
            ("fatal", index)
            if issue.severity == Severity.FATAL
            else (issue.source, issue.code, issue.severity, issue.source_document)
        )
        groups.setdefault(key, []).append(issue)
    grouped: list[ValidationIssue] = []
    for members in groups.values():
        first = members[0]
        update: dict[str, object] = {}
        if extraction_revision is not None:
            update["extraction_revision"] = extraction_revision
        if len(members) > 1:
            update["details"] = {
                **first.details,
                "occurrences": len(members),
                "samples": [
                    {"line": item.line, "column": item.column, "message": item.message}
                    for item in members[:MAX_SAMPLES]
                ],
            }
        grouped.append(first.model_copy(update=update) if update else first)
    return tuple(grouped)
