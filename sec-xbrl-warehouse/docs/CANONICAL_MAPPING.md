# Canonical Mapping Policy

## Purpose

Canonical metrics make cross-company queries convenient, but they are interpretation, not source truth. Raw concepts and facts always remain queryable.

## Precedence

A mapping's `namespace` is either an exact namespace URI or a family token:
`us-gaap`, `ifrs-full`, `dei`, or `srt`. A family mapping matches every *standard*
namespace URI of that family (`http://fasb.org/us-gaap/<date>`,
`https://xbrl.ifrs.org/taxonomy/<date>/ifrs-full`, `http://xbrl.sec.gov/dei/<year>`,
`http://fasb.org/srt/<year>`), so one approval covers every taxonomy year. Extension
and look-alike namespaces never match a family.

Among approved mappings valid on the filing date, the most specific wins:

1. CIK (filing-specific).
2. Taxonomy version.
3. Exact namespace URI.
4. Namespace family.

Without an approved mapping, configured candidates and filing evidence go to an
unmapped review item.

## Metrics and seed candidates

`config/canonical_metrics.yml` defines about 45 metrics. Formula metrics (for example
`free_cash_flow`) use `statement: derived`, reference only other metrics, and have no
concept candidates; plan 11 computes them.

`config/concept_seed.yml` lists candidate concepts per metric with a `namespace_hint`
(`us-gaap`, `ifrs-full`, `dei`, `srt`) and a `priority` (1 = preferred within that
metric and family). No concept is a candidate for two metrics. Seeds are only
candidates: nothing is approved until a person runs

```bash
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text> --dry-run
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text> [--metric revenue]
```

which approves each selected candidate as a family mapping and records the reviewer
and rationale. `--dry-run` writes nothing and marks each candidate a real run with the
same reviewer and rationale would approve. Mappings are append-only and versioned: repeating an identical
approval (same scope, validity, reviewer, and rationale) returns the existing version;
a new reviewer or rationale records a new version.

## Applying approvals to loaded filings

```bash
xbrl-warehouse mapping remap --epoch 2026-10-05 [--accession <accession>]
xbrl-warehouse worker run --job-type map_canonical
```

`remap` enqueues one `map_canonical` job per filing with a published extraction
revision. The payload is `{accession, extraction_revision}`; the worker loads the
facts from that revision's saved (silver) extraction, verified by checksum, and
inserts canonical facts. The idempotency key includes the epoch, so the same epoch
enqueues nothing new and a new epoch maps every filing again. Canonical facts are
idempotent per raw fact, mapping version, and extraction revision.

## Prohibited shortcuts

- Mapping from a fuzzy English label alone.
- Ignoring dimensions when selecting consolidated versus segment facts.
- Treating all duration facts as interchangeable.
- Applying a sign flip without a versioned rule and explanation.
- Replacing an old decision in place.

## Review evidence

A review should show concept namespace/name, taxonomy version, labels/docs, unit, period, dimensions, statement role and line, calculation parents/children, comparable filers, and the proposed metric/sign rule.

## Review queue

There is at most one open review per concept, identified by
`(concept_namespace, concept_name)`; the same name in an extension namespace is a
separate concept. `CanonicalMapper.map_batches` sends one request per concept per
filing, after the last batch, with `fact_count` (unmapped facts of that concept in
the filing) and up to five `sample_fact_ids`.

- If the concept already has an open review, the new filing replaces its decision
  evidence and points `accession`/`fact_id` at its first sample.
- Per-filing counts are stored in `mapping_review_observations`, keyed by review and
  accession, so replaying a filing doesn't inflate them. The review's `evidence`
  carries the cumulative `fact_count`, `filing_count`, and `latest_accession`.
- `xbrl-warehouse mapping reviews` lists open reviews with those counts.
- Resolving still requires an approved mapping for the review's concept. A later
  unmapped fact for a resolved concept opens a new review.
- Migration `0003_mapping_review_per_concept` collapsed existing open reviews: the
  earliest open review per concept was kept, and the others were marked `superseded`
  with `resolution.superseded_by` (not deleted). Downgrade reopens them.

## Reproducibility

Every canonical fact stores raw fact ID, mapping UUID/version, confidence, explanation, extraction revision, and normalized unit/value. Re-running with a historical mapping version must reproduce the prior result.
