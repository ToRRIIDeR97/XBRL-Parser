# Schema Evolution

## SEC source schemas

Treat source headers and order as versioned external contracts. Any unknown/missing/reordered column creates a captured difference. Do not silently drop an unfamiliar field.

## Arrow/Parquet schemas

Assign an internal schema version to every silver dataset. Additive nullable fields can use a new version and compatibility view; destructive type changes require a new dataset path and replay.

## ClickHouse

- Add columns with explicit defaults where safe.
- Create new tables/views for incompatible key or precision changes.
- Do not rely on background `ReplacingMergeTree` merges for query correctness.
- Keep DDL numbered and idempotent.
- `xbrl-warehouse db upgrade-clickhouse --yes` (through the `worker` service) rebuilds
  each table whose column types or sort key changed: it creates the new table, copies
  every physical row with merges stopped, checks row counts, swaps the tables, and
  keeps the old one as `<table>__backup_<UTC timestamp>_<token>`. Backups are never
  dropped automatically; drop them only after verifying the upgrade.

### Binary hashes and concept split (plan 14)

Upgrading from the hex-hash schema (main @ 8f16ca0 and plans 10-13):

- Hash columns go from `FixedString(64)`/`String` hex to `FixedString(32)` bytes
  (`unhex`); blank values become zero bytes. A value that isn't a hex digest (never
  written by this pipeline) becomes its SHA-256, the same mapping new writes use, so
  joins between tables still match.
- `xbrl_concepts` keeps extension rows; its standard rows are copied into
  `taxonomy_concepts` (deduplicated). Standard concepts a published filing labels
  itself (label arc in its own documents) are copied with `labels_json = '[]'`, and
  their labels go to `xbrl_concept_labels` for that filing; unpublished revisions
  share no labels. The backup keeps every original row, and the row-count check
  compares extension rows.
- `xbrl_facts` gains `raw_value_scale` (NULL for existing rows, so their stored raw
  values are returned as before). Only new writes drop raw values that equal the
  typed value.
- Views are recreated after the swaps; their output is unchanged
  (`tests/integration/test_clickhouse_slimming_upgrade.py` compares reader results
  before and after on a database created with main's DDL).

Steps: back up (BACKUP_RESTORE.md), stop workers, run `doctor` (it reports the drift),
check free space (at least 110% of the affected tables), run
`db upgrade-clickhouse --yes`, then `doctor` again. Grant `xbrl_api` `SELECT` on
`taxonomy_concepts`, `xbrl_concept_labels`, and `v_filing_concepts_latest`.

## PostgreSQL

Use Alembic for every control-plane change. Migrations must be reversible unless data volume or semantics make reversal unsafe; document those exceptions.

## Parser versions

A new Arelle/extraction implementation increments `extraction_revision`. It does not replace package bytes or old extraction lineage. Reprocess a representative corpus before broad rollout.
