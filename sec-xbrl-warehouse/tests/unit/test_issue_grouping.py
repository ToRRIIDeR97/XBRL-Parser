from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import ValidationIssue
from sec_xbrl_warehouse.validation.issue_grouping import group_issues


def _issue(field: str, values: list[str]) -> ValidationIssue:
    return ValidationIssue(
        source="warehouse",
        code="warehouse:fiscalFocusConflict",
        severity=Severity.ERROR,
        message=f"Conflicting values for {field}",
        accession="0000000001-26-000001",
        details={"field": field, "values": values},
    )


def test_grouped_samples_keep_each_occurrences_details() -> None:
    (grouped,) = group_issues(
        [_issue("fiscal_year", ["2024", "2025"]), _issue("report_date", ["2025-12-31", "x"])],
        extraction_revision=3,
    )
    assert grouped.details["occurrences"] == 2
    assert grouped.details["field"] == "fiscal_year"
    assert [sample["details"]["field"] for sample in grouped.details["samples"]] == [
        "fiscal_year",
        "report_date",
    ]
    assert grouped.extraction_revision == 3
