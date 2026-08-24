from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.domain.models import ValidationIssue
from sec_xbrl_warehouse.exceptions import XbrlExtractionError
from sec_xbrl_warehouse.validation.xbrl_validation import (
    ArelleIssueNormalizer,
    reject_configured_issues,
    reject_fatal_issues,
    summarize_issues,
)


def test_normalizer_maps_arelle_records_with_codes_and_location() -> None:
    normalizer = ArelleIssueNormalizer()
    issues = normalizer.normalize(
        [
            {
                "level": "error",
                "code": ["XBRLError", "8.3.7"],
                "message": "Invalid value",
                "file": "simple.xml",
                "line": 17,
                "column": 5,
                "details": {"args": ("a", "b")},
            },
            {"level": "warning", "code": "ARELLE_UNCLASSIFIED", "message": "note"},
        ],
        accession="0000000001-26-000001",
    )
    assert len(issues) == 2
    first = issues[0]
    assert first.code == "['XBRLError', '8.3.7']"
    assert first.severity is Severity.ERROR
    assert first.source_document == "simple.xml"
    assert first.line == 17
    assert first.column == 5
    assert first.accession == "0000000001-26-000001"
    assert first.details == {"args": "('a', 'b')"}
    assert issues[1].severity is Severity.WARNING


def test_normalizer_truncates_long_messages_and_retains_original() -> None:
    normalizer = ArelleIssueNormalizer(max_message_chars=50)
    issue = normalizer.normalize_one(
        {"level": "warning", "message": "x" * 200},
        accession="a",
    )
    assert issue is not None
    assert len(issue.message) == 50
    assert issue.details["truncated_message"] == "x" * 50


def test_normalizer_can_drop_non_fatal_when_configured() -> None:
    normalizer = ArelleIssueNormalizer(
        retain_all_messages=False, reject_severities=(Severity.FATAL,)
    )
    issues = normalizer.normalize(
        [
            {"level": "info", "message": "info msg"},
            {"level": "fatal", "message": "fatal msg"},
        ]
    )
    assert len(issues) == 1
    assert issues[0].severity is Severity.FATAL


def test_reject_fatal_issues_raises_on_fatal() -> None:
    issues = [
        ValidationIssue(
            source="arelle",
            code="ARELLE_FATAL",
            severity=Severity.FATAL,
            message="boom",
        )
    ]
    try:
        reject_fatal_issues(issues)
        raised = False
    except XbrlExtractionError:
        raised = True
    assert raised


def test_reject_fatal_issues_passes_when_only_warnings() -> None:
    issues = [
        ValidationIssue(
            source="arelle",
            code="ARELLE_WARN",
            severity=Severity.WARNING,
            message="warn",
        )
    ]
    reject_fatal_issues(issues)  # must not raise


def test_reject_configured_issues_filters_by_severity() -> None:
    issues = [
        ValidationIssue(
            source="arelle",
            code="c1",
            severity=Severity.WARNING,
            message="w",
        ),
        ValidationIssue(
            source="arelle",
            code="c2",
            severity=Severity.ERROR,
            message="e",
        ),
        ValidationIssue(
            source="arelle",
            code="c3",
            severity=Severity.FATAL,
            message="f",
        ),
    ]
    rejected = reject_configured_issues(issues, (Severity.ERROR, Severity.FATAL))
    assert [item.code for item in rejected] == ["c2", "c3"]


def test_summarize_issues_groups_by_severity_and_code() -> None:
    issues = [
        ValidationIssue(source="a", code="X1", severity=Severity.WARNING, message="m"),
        ValidationIssue(source="a", code="X1", severity=Severity.WARNING, message="m"),
        ValidationIssue(source="a", code="X2", severity=Severity.ERROR, message="m"),
    ]
    assert summarize_issues(issues) == {"warning:X1": 2, "error:X2": 1}
