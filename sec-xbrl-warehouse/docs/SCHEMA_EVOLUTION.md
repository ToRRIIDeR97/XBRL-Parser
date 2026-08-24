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

## PostgreSQL

Use Alembic for every control-plane change. Migrations must be reversible unless data volume or semantics make reversal unsafe; document those exceptions.

## Parser versions

A new Arelle/extraction implementation increments `extraction_revision`. It does not replace package bytes or old extraction lineage. Reprocess a representative corpus before broad rollout.
