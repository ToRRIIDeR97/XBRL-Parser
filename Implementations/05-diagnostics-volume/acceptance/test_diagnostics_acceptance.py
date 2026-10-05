"""Worker-visible acceptance tests for 05-diagnostics-volume."""

from __future__ import annotations

import asyncio

from support05 import MappingRepository, fact, filing, ingest, issue

from sec_xbrl_warehouse.domain.canonical import CanonicalMetric
from sec_xbrl_warehouse.mapping.canonical_mapper import CanonicalMapper


# AC-1
def test_ac1_recorded_issues_carry_extraction_revision() -> None:
    control = ingest((issue("xbrl:calcInconsistency", "example-20251231.htm", 10),), revision=3)
    assert control.issues
    assert {item.extraction_revision for item in control.issues} == {3}


# AC-2
def test_ac2_repeated_issues_collapse_with_counts_and_bounded_samples() -> None:
    noisy = tuple(issue("xmlSchema:syntax", "ex31.htm", line) for line in range(1, 1001))
    other = (issue("ix11.12.1.2:missingReferences", "example-20251231.htm", 7),)
    control = ingest(noisy + other)

    by_code = {item.code: item for item in control.issues}
    assert set(by_code) == {"xmlSchema:syntax", "ix11.12.1.2:missingReferences"}
    syntax = by_code["xmlSchema:syntax"]
    assert syntax.details["occurrences"] == 1000
    assert 1 <= len(syntax.details["samples"]) <= 5
    assert by_code["ix11.12.1.2:missingReferences"].details.get("occurrences", 1) == 1


# AC-4
def test_ac4_one_review_request_per_concept_per_filing() -> None:
    repository = MappingRepository()
    metric = CanonicalMetric(
        code="revenue",
        name="Revenue",
        statement="income_statement",
        period_type="duration",
        unit_family="monetary",
    )
    mapper = CanonicalMapper({"revenue": metric}, {("us-gaap", "Revenues"): ("revenue",)}, repository)
    facts = [fact("Revenues", f"r{i}") for i in range(3)] + [fact("CostOfRevenue", "c1")]

    asyncio.run(mapper.map_batch(facts, filing(), extraction_revision=1))

    concepts = [request[0].concept_name for request in repository.requests]
    assert sorted(concepts) == ["CostOfRevenue", "Revenues"]
    revenue = next(request for request in repository.requests if request[0].concept_name == "Revenues")
    assert revenue[2]["fact_count"] == 3
