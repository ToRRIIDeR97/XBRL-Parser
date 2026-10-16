# 14 — Slim the ClickHouse schema: shared taxonomy concepts, binary hashes, conditional raw values

Status: implemented and merged to main.

## Problem

Measured in ClickHouse for 43 AAPL filings (5 October 2026). The total is 118 MB
compressed:

- **`xbrl_concepts` is the largest table (76 MB, 821,800 rows), five times the facts.**
  Each filing stores its whole taxonomy (about 19,500 concepts). 736,481 rows (90%)
  are standard US-GAAP/dei/srt concepts repeated per filing. The facts use only 756
  distinct concepts.
- **Hex SHA-256 strings don't compress.** `_row_hash` alone is 50.3 MB of the 76 MB
  concepts table. In `xbrl_facts`, `fact_id`, `_row_hash` and `duplicate_set_hash` are
  about 1.5 MB each (about 35% of the table), and `relationship_id`/`_row_hash` are
  14 MB of `xbrl_relationships`. A 64-character hex string stores 32 bytes of
  information.
- **`raw_value` repeats typed values.** `raw_value` (32 MB uncompressed) equals
  `value_text` (32 MB) for most text facts, and equals the decimal rendering for
  plain numbers.

### Before / after (43 filings)

| | Before | After (estimate) |
|---|---|---|
| `xbrl_concepts` rows | 821,800 | about 85,000 extension rows; standard concepts once per taxonomy version |
| Hash columns | `FixedString(64)` hex | `FixedString(32)` binary (or `UInt64` for internal row-dedupe hashes); views return hex |
| `raw_value` | always stored | stored only when it differs from the typed value |
| ClickHouse total | 118 MB | about 30–40 MB |

## Scope

Likely files:

- `infrastructure/storage/clickhouse.py`:
  - `concepts_table` keeps `is_extension` concepts;
  - new `taxonomy_concepts_table(bundle)` for standard concepts;
  - insert both;
  - convert hex hashes to binary at the ClickHouse boundary (`unhex` on insert, or
    Arrow `fixed_size_binary(32)` for ClickHouse batches only);
  - apply the `raw_value` rule in `facts_table`.
- `sql/clickhouse/020_full_xbrl_tables.sql`, `040_views.sql`, plus a new file:
  - `taxonomy_concepts` (`ReplacingMergeTree`, ordered by
    `(concept_namespace, concept_name, taxonomy_version)`);
  - `v_filing_concepts_latest` = the filing's extension concepts ∪ the taxonomy
    concepts its facts reference;
  - `FixedString(32)` hash columns, with views exposing `lower(hex(...))` under the
    same column names so query results and the API don't change.
- The `db upgrade-clickhouse` path: type and sort-key changes rebuild tables with
  timestamped backups, as it already does for incompatible sort keys.
- `query/service.py`: unchanged results. `docs/DATA_MODEL.md` and
  `docs/SCHEMA_EVOLUTION.md` describe the encoding and the upgrade.

`raw_value` rule:

- keep `raw_value` unless it equals `value_text` (text facts) or `str(value_decimal)`
  (numeric);
- always keep it for nil, boolean and date facts, and when the typed value is `None`.

Exclusions:

- Parquet silver keeps hex strings and full concepts. It's the rebuild source and
  stays lossless.
- Plan 13 covers object storage.

Invariants:

- **Fact identity is unchanged.** The same SHA-256 values, only encoded as bytes
  inside ClickHouse. Replay and dedupe give the same logical rows.
- `xbrl_facts` and canonical lineage queries keep returning hex IDs.
- Every raw value stays recoverable: either stored, or exactly equal to the typed
  value.
- ClickHouse remains rebuildable from silver.

## Acceptance criteria

- **AC-1 Concept split.** `concepts_table(bundle)` contains only extension concepts.
  `taxonomy_concepts_table(bundle)` contains the standard ones.
- **AC-2 Binary hashes.** In ClickHouse, `xbrl_facts.fact_id` is `FixedString(32)`
  (similarly the other identity hashes). `_row_hash` is `FixedString(32)` or `UInt64`.
  `v_xbrl_facts_latest.fact_id` returns the same 64-character hex string the bundle had.
- **AC-3 Conditional raw value.**
  - Dropped when equal to the typed value: text `"Plain text"`, numeric `"1000"` and
    `"1.50"`.
  - Kept when it carries information: `"1,234"`, `"(5)"`, HTML text blocks.
- **AC-4 Dedupe across filings.** Two filings with the same standard concepts store
  them once (`taxonomy_concepts FINAL`). `v_filing_concepts_latest` lists, per filing,
  its extensions plus the standard concepts its facts use.

## Implementation sequence

1. `raw_value` rule and concept split in Arrow conversion (AC-1, AC-3). Unit tests.
2. DDL, upgrade path and views (AC-2, AC-4). Test `db upgrade-clickhouse --yes` on a
   copy of the local volume. Backups remain.
3. Keep query and API outputs identical (existing API tests, plus an integration test
   comparing a query before and after).
4. Docs. Validate suite including integration. Re-measure `system.parts` and record
   the before/after sizes in the PR.

## Verification

From `sec-xbrl-warehouse/`, with local storage services running:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/14-clickhouse-schema-slimming/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/14-clickhouse-schema-slimming/verification
```

Sizes:

```sql
SELECT table, sum(rows), formatReadableSize(sum(data_compressed_bytes))
FROM system.parts WHERE active AND database = 'xbrl' GROUP BY table
```

### Baseline (main @ 8f16ca0, 5 October 2026)

6 failed, 3 passed:

- AC-1: `taxonomy_concepts_table` is missing.
- AC-2: the column is `FixedString(64)`.
- AC-3: equal raw values are still stored.
- AC-4: `taxonomy_concepts` is unknown.
- Passing on baseline: the "keep" cases (`1,000`, `(5)`, HTML), which pin behavior.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- **Hashes (AC-2).** Every identity hash and `_row_hash` on the XBRL, statement,
  disclosure, and canonical tables is `FixedString(32)` (arrays
  `Array(FixedString(32))`: `footnote_ids`, `linked_fact_ids`, `value_fact_ids`).
  `encode_hash_columns` converts hex to bytes at the ClickHouse insert boundary only;
  silver Parquet is unchanged. `v_xbrl_facts_latest`, `v_canonical_facts_latest`,
  `v_statement_lines_latest`, `v_cover_pages_latest`, and `v_segment_facts_latest`
  return `lower(hex(...))` under the same names (a tuple fact's blank `context_hash`
  reads back as `''`). `metric_series`, `companies`, and the SEC bulk tables have no
  identity hashes to convert (bulk `_row_hash` stays hex, out of scope). A non-hex
  identifier (none are produced by the pipeline; some plan tests use them) is stored
  as its SHA-256, identically on insert and upgrade.
- **Raw values (AC-3).** `facts_table` drops `raw_value` when it equals
  `str(value_decimal)` in plain notation (≤ 18 fraction digits) or `value_text`, and
  records `raw_value_scale`; the view rebuilds the exact string (`"1.50"` included).
  `silver_facts_table` keeps every raw value.
- **Concepts (AC-1, AC-4).** `concepts_table` = extensions; `taxonomy_concepts_table` =
  standard concepts → `taxonomy_concepts` (ReplacingMergeTree by `_ingested_at`,
  ordered by namespace, name, version). `v_filing_concepts_latest` = extensions ∪
  standard concepts the facts use (matched on namespace and name; the namespace URI
  carries the year). `silver_concepts_table` keeps every concept. Publish row counts
  cover extension rows.
- **Upgrade.** `upgrade_ddl` decodes hex hash columns while copying, moves standard
  concept rows to `taxonomy_concepts`, and names backups
  `<table>__backup_<UTC timestamp>_<token>`. Tested on temporary databases only: a DB
  built from main's DDL (`tests/fixtures/clickhouse_ddl_main`, copied from
  `main` @ 8f16ca0) with sample rows returns identical `QueryService.raw_facts`,
  `canonical_series`, `filing_summary`, and a fact-to-canonical lineage join before
  and after; a DB with hex-encoded plan 10-12 tables returns identical rows.
- **Readers.** QueryService, reconciliation, series rebuild, statement and disclosure
  views, and remap (writes through `insert_canonical_facts`) all go through the views
  or the encoding boundary; the full suite passes.
- **Sizes** (one AAPL 10-K, temporary databases, after `OPTIMIZE FINAL`): old encoding
  3.04 MB (concepts 1.88 MB, relationships 0.79 MB, facts 0.35 MB); new 2.03 MB, of
  which 1.26 MB is `taxonomy_concepts`, written once for all filings using that
  taxonomy. Per additional filing: about 0.77 MB instead of 3.04 MB; 43 filings
  ≈ 34 MB instead of ≈ 130 MB.
- Docs: DATA_MODEL (encoding, concepts), SCHEMA_EVOLUTION (upgrade steps),
  ARCHITECTURE, SECURITY (`xbrl_api` grants). `doctor` expects
  `v_filing_concepts_latest`.
- Operator follow-up: back up, stop workers, `doctor`, `db upgrade-clickhouse --yes`,
  `doctor`, grant `xbrl_api` on the new objects; drop the `__backup_*` tables only
  after verifying.

### Follow-up (verifier, 5 October 2026)

- **Filer labels no longer leak across filings.** SEC filings don't load the standard
  label linkbases, so a standard concept's labels in a bundle come from label arcs; a
  concept is filer-labelled when the filing has a label arc for it in one of its own
  documents (`metadata.source_documents`). Those concepts' full label sets go to the
  new per-filing `xbrl_concept_labels` table (concept granularity: the filing's exact
  `labels_json`, which keeps the view's output identical to what the filing had);
  `taxonomy_concepts` stores `[]` with `labels_rank` 0 for them, and taxonomy-owned
  labels with rank 1 (`ReplacingMergeTree(labels_rank)`). `v_filing_concepts_latest`
  uses the filing's own labels where present. The upgrade applies the same rule from
  `xbrl_relationships` and the published extraction's `source_documents`.
  `xbrl_concept_labels` row counts are checked at publish.
- Tests: a two-filer integration test (same us-gaap concept, different labels), and
  the main-DDL upgrade test now includes a filer label arc. Verifier probes rerun from
  `scratchpad/impl/` on temporary databases: `test_labels.py` shows "Net sales" for
  Apple and "Total revenue (Other Corp)" only for the other filer; `phase.py` (plan 13
  code → upgrade) gives 26 of 26 result sets identical, 9.11 → 4.89 MB.
- Non-hex identifiers keep the SHA-256 mapping (plan 10/11 tests use them); documented
  as test-only in DATA_MODEL.md, with a `non_hex_identifier_hashed` warning outside
  `XBRL_ENV=test`.
