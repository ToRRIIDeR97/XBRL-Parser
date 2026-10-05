"""Independent verification tests for 05-diagnostics-volume."""

from __future__ import annotations

import asyncio

from support05v import MappingRepository, fact, filing, ingest, issue

from sec_xbrl_warehouse.domain.canonical import CanonicalMetric
from sec_xbrl_warehouse.domain.enums import Severity
from sec_xbrl_warehouse.exceptions import XbrlExtractionError
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper


# AC-3: grouping never merges different documents or severities, and keeps first location.
def test_ac3_grouping_key_is_source_code_severity_and_document() -> None:
    issues = (
        issue("xmlSchema:syntax", "ex31.htm", 5),
        issue("xmlSchema:syntax", "ex31.htm", 9),
        issue("xmlSchema:syntax", "ex32.htm", 3),
        issue("xmlSchema:syntax", "ex31.htm", 11, severity=Severity.WARNING),
    )
    control = ingest(issues)
    groups = {(item.source_document, item.severity.value): item for item in control.issues}
    assert set(groups) == {("ex31.htm", "error"), ("ex32.htm", "error"), ("ex31.htm", "warning")}
    first = groups[("ex31.htm", "error")]
    assert first.details["occurrences"] == 2
    assert first.line == 5
    assert [sample["line"] for sample in first.details["samples"]] == [5, 9]


# AC-3: fatal issues are never folded into a group; each one is recorded before rejection.
def test_ac3_fatal_issues_stay_individual() -> None:
    issues = tuple(
        issue("warehouse:schemaDrift", "example-20251231.htm", line, severity=Severity.FATAL)
        for line in (1, 2)
    )
    control = ingest(issues, expect=XbrlExtractionError)
    fatal = [item for item in control.issues if item.severity == Severity.FATAL]
    assert len(fatal) == 2


# AC-1: warehouse-generated issues are tagged too, and replay with a new revision is distinguishable.
def test_ac1_revision_distinguishes_replays() -> None:
    first = ingest((issue("xbrl:calcInconsistency", "a.htm", 1),), revision=1)
    second = ingest((issue("xbrl:calcInconsistency", "a.htm", 1),), revision=2)
    assert {item.extraction_revision for item in first.issues} == {1}
    assert {item.extraction_revision for item in second.issues} == {2}


# AC-4: dedupe survives batch boundaries and caps sample ids.
def test_ac4_review_dedupe_spans_batches_and_caps_samples() -> None:
    repository = MappingRepository()
    metric = CanonicalMetric(
        code="revenue", name="Revenue", statement="income_statement", period_type="duration", unit_family="monetary"
    )
    mapper = CanonicalMapper({"revenue": metric}, {}, repository)
    facts = [fact("Revenues", f"r{i}") for i in range(12)]

    async def run() -> None:
        async for _ in mapper.map_batches(facts, filing(), extraction_revision=1, batch_rows=4):
            pass

    asyncio.run(run())
    assert len(repository.requests) == 1
    evidence = repository.requests[0][2]
    assert evidence["fact_count"] == 12
    assert 1 <= len(evidence["sample_fact_ids"]) <= 5


# AC-4: the same concept name in different namespaces stays separate (extension vs standard).
def test_ac4_namespace_is_part_of_review_identity() -> None:
    repository = MappingRepository()
    metric = CanonicalMetric(
        code="revenue", name="Revenue", statement="income_statement", period_type="duration", unit_family="monetary"
    )
    mapper = CanonicalMapper({"revenue": metric}, {}, repository)
    facts = [fact("Revenues", "std"), fact("Revenues", "ext", namespace="http://example.com/2025")]
    asyncio.run(mapper.map_batch(facts, filing(), extraction_revision=1))
    assert len(repository.requests) == 2
