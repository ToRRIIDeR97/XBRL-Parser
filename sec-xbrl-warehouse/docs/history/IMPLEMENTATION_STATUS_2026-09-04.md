# Implementation Status

All six implementation phases are complete. Checkmarks mean the code path exists and has an automated regression check; they do not claim that production data has been downloaded.

## Phase A — Foundation

- [x] Validated settings, SEC identity, SQL identifiers, and secrets boundaries.
- [x] PostgreSQL migrations and preserving ClickHouse schema application/upgrades.
- [x] Immutable local/S3 object stores and fenced PostgreSQL jobs.
- [x] Actionable `doctor` checks for configuration, disk, schemas, and services.

## Phase B — Historical bulk path

- [x] Catalog snapshots and source artifact revisions.
- [x] Resumable acquisition with HTTP validators and checksums.
- [x] Bounded ZIP/TSV parsing with explicit Arrow schemas and drift quarantine.
- [x] Deterministic silver Parquet and replay-safe ClickHouse writes.
- [x] Batch checkpoints, source/silver/gold reconciliation, and replay.

## Phase C — Arelle path

- [x] Synthetic instance, schema, presentation, calculation, label, dimension, and footnote extraction.
- [x] Facts, contexts, units, concepts, networks, lineage, duplicates, and validation issues.
- [x] Offline policy, allowed taxonomy hosts, cache/package directories, and multi-document packages.
- [x] Parser-versioned publication and deterministic archive replay.

## Phase D — Incremental orchestration

- [x] Company and daily-index discovery with retained source indexes.
- [x] Checksum-addressed filing-package acquisition and cache reuse.
- [x] Download, extraction, silver, warehouse, publication, and canonical checkpoints.
- [x] Filing-level bulk/incremental reconciliation.

## Phase E — Canonical layer

- [x] Metric seed import and versioned mapping specificity.
- [x] Approval/review workflow with idempotent review creation.
- [x] Derived formulas with raw-fact and mapping lineage.
- [x] Bounded raw/canonical query APIs and replay-safe latest views.

## Phase F — Operations

- [x] API and worker Prometheus endpoints plus a provisioned Grafana dashboard.
- [x] Idempotent rolling discovery command for cron/systemd scheduling.
- [x] Backup/restore drill procedure and immutable rebuild source definition.
- [x] Preserving ClickHouse upgrade and revisioned reprocessing workflow.
- [x] Capacity guardrails and staged worker tuning procedure.

## External production acceptance

These are deployment/data exercises, not missing implementation:

- run the real-filing corpus described in `TEST_PLAN.md` with an authorized SEC identity;
- complete a 2016-to-current FSNDS canary and then the full backfill;
- independently reparse and reconcile at least 100 sampled filings;
- perform the backup/restore drill in the target infrastructure and record recovery time/data-loss results;
- add TLS, API authentication, managed secrets, alert routing, and object replication for the chosen production platform.
