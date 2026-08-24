# Implementation Order

## Phase A — Foundation

- Validate settings and secrets.
- Apply PostgreSQL/ClickHouse schemas.
- Complete object-store and job repository integration tests.
- Make `doctor` exhaustive and actionable.

## Phase B — Historical bulk path

- Catalog sync and artifact versioning.
- Resumable acquisition and checksums.
- Streaming TSV parser and schema registry.
- Silver Parquet writes.
- ClickHouse bulk tables.
- Artifact reconciliation and replay.

## Phase C — Arelle path

- Synthetic fixture extraction.
- Facts/contexts/concepts.
- Networks and footnotes.
- Validation issue normalization.
- Full package/multi-document handling.
- Real-filing corpus.

## Phase D — Incremental orchestration

- Daily index discovery.
- Filing package downloader.
- Worker stage checkpoints.
- Backfill/incremental reconciliation.

## Phase E — Canonical layer

- Metric seed import.
- Exact approved mappings.
- Review queue and explanations.
- Derived metrics.
- Query views and API.

## Phase F — Operations

- Metrics and dashboards.
- Scheduled runs.
- Backups and restore drills.
- Upgrade/reprocessing workflow.
- Capacity tuning.
