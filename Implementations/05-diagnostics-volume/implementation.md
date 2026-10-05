# 05 — Make validation issues and mapping reviews usable

Status: plan only. Code follows after this plan is reviewed.

## Problem

Measured on the local warehouse (43 AAPL filings, 5 October 2026):

- `validation_issues` holds 162,803 rows (95 MB). The issues aren't tied to an
  extraction revision. It's impossible to tell whether the 588
  `webCache:retryingOperation` errors are stale revision-1 diagnostics or affect the
  live revision-2 data. Their timestamps suggest stale, but the table can't prove it.
- One noisy code dominates: 119,117 `xmlSchema:syntax` rows, one per exhibit line.
  Plan 01 removes the cause, but any repeated diagnostic still produces one row per
  occurrence.
- `mapping_reviews` holds 41,333 rows (40 MB): one open review per unmapped *fact*.
  The facts table has only 756 distinct concepts, and reviewers decide mappings per
  concept. `concept_mappings` is empty, so every fact of every filing becomes a
  review ([canonical_mapper.py:258](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/mapping/canonical_mapper.py)).

### Before / after

| | Before | After |
|---|---|---|
| 1,000 `xmlSchema:syntax` lines in one exhibit | 1,000 rows | 1 row, `details.occurrences = 1000`, up to 5 sample locations |
| Which revision raised an issue? | unknown | `extraction_revision` column |
| Reviews for 3 unmapped `Revenues` facts in a filing | 3 | 1 (`evidence.fact_count = 3`) |
| Reviews for the same concept across 43 filings | one per fact per filing | one open review per concept, evidence updated |

## Scope

Likely files:

- `domain/models.py`: `ValidationIssue.extraction_revision: int | None = None`.
- `ingestion/incremental.py`: tag issues from an extraction with its revision. Group
  them before `record_validation_issues`. Apply the same grouping to warehouse-raised
  issues, such as those added by plan 02.
- A pure grouping helper (`validation/` package).
- `infrastructure/storage/postgres_models.py`, a new Alembic migration and
  `postgres.py`: `validation_issues.extraction_revision` (nullable, indexed with
  `accession`).
- `mapping/canonical_mapper.py`: one review request per
  `(concept_namespace, concept_name)` per filing, across batches.
- `mapping/postgres_repository.py` plus a migration: review identity becomes the
  concept, unique on `(concept_namespace, concept_name)` while `state = 'open'`. A
  new filing updates the open review's evidence (latest accession, cumulative fact
  count) instead of inserting rows. Existing rows are collapsed by the migration.
  Resolved reviews are kept.
- `cli.py` `mapping reviews` output and `docs/CANONICAL_MAPPING.md`.

Exclusions:

- Bulk-load (`artifact_id`) issues keep their current shape. Grouping applies only to
  filing extractions.
- No change to which severities reject a filing (`config/validation.yml`).

Invariants:

- Issues are recorded before the reject decision, as today. Grouping must not hide a
  rejecting issue.
- Mapping decisions are unchanged. Only the number of review requests changes.
- Resolving a review still requires an approved mapping whose concept matches the
  review (existing `resolve_review` check).

## Acceptance criteria

- **AC-1 Revision tag.** Every issue recorded during `ingest(filing, extraction_revision=N)`
  has `extraction_revision == N`, whether Arelle or the warehouse raised it. Two
  replays with different revisions are distinguishable.
- **AC-2 Grouping.** Issues sharing `(source, code, severity, source_document)` within
  one extraction are recorded as one issue. It has `details.occurrences` (the count)
  and `details.samples` (1–5 entries, in input order, each with at least `line`,
  `column` and `message`). The first occurrence's message, line and column are the
  row's own fields. A single occurrence needs no `occurrences` key, or a value of 1.
- **AC-3 Grouping boundaries.** Different documents, codes or severities are never
  merged. `fatal` issues are never grouped: each is recorded, before rejection.
- **AC-4 Review per concept.** `CanonicalMapper.map_batches` emits at most one review
  request per `(concept_namespace, concept_name)` per filing, even across batch
  boundaries. The request's evidence includes `fact_count` (unmapped facts of that
  concept in the filing) and `sample_fact_ids` (1–5). The same name in different
  namespaces (extension versus standard) gives separate requests.
- **AC-5 Storage.** Two migrations: (a) `validation_issues.extraction_revision`;
  (b) collapse the open `mapping_reviews` to one per concept, then switch to the
  concept uniqueness rule. `alembic upgrade head` and `downgrade -1` both work on a
  copy of the current local database. After the upgrade, `mapping reviews` lists at
  most one open row per concept.

## Implementation sequence

1. Domain field and grouping helper (AC-1 to AC-3). Wire them into `ingest`.
2. Mapper dedupe (AC-4).
3. Migrations and repository changes (AC-5). Update `mapping reviews` CLI output.
4. Docs (`DATA_MODEL.md`, `CANONICAL_MAPPING.md`, `INGESTION_RUNBOOK.md` queries).
   Run the Validate suite, including the integration run for the migrations.
5. Operator step: after re-extraction (plans 01–03), check
   `SELECT extraction_revision, code, sum((details->>'occurrences')::int) ...`
   to confirm which diagnostics belong to the live revision.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/05-diagnostics-volume/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/05-diagnostics-volume/verification
```

The tests use fake control and mapping repositories through the public
`IncrementalFilingIngestor.ingest` and `CanonicalMapper.map_batch(es)` entry points.
AC-5 is checked manually, plus an integration test added with the implementation:
run the migration on a scratch copy of the local database and compare row counts
before and after.

### Baseline (main @ 8f16ca0, 5 October 2026)

6 failed, 2 passed:

- AC-1 fails because `ValidationIssue` has no `extraction_revision`.
- AC-2 and AC-3 grouping fail because there's no `occurrences` key.
- AC-4 fails with 4 requests instead of 2, and 12 instead of 1.
- `test_ac3_fatal_issues_stay_individual` and
  `test_ac4_namespace_is_part_of_review_identity` pass on baseline. They pin
  behavior the change must keep.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
