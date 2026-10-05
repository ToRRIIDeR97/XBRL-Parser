# Canonical Mapping Policy

## Purpose

Canonical metrics make cross-company queries convenient, but they are interpretation, not source truth. Raw concepts and facts always remain queryable.

## Precedence

1. Approved filing-specific concept mapping.
2. Approved exact namespace, concept, and taxonomy-version mapping.
3. Approved namespace/concept mapping valid for the filing date.
4. Filing presentation/calculation evidence and configured candidates.
5. Unmapped review item.

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
