# Ingestion Runbook

## Historical backfill

1. Sync the SEC bulk catalog.
2. Generate a deterministic plan for the desired period.
3. Review expected artifact count and download size.
4. Enqueue one job per artifact revision.
5. Start one worker and validate the first artifact end to end.
6. Increase workers only after memory, disk, and ClickHouse insert behavior are measured.
7. Reconcile source ZIP row counts, Parquet counts, and ClickHouse counts.
8. Record completion and validation state in PostgreSQL.

Example commands:

```bash
xbrl-warehouse catalog sync --dataset notes
xbrl-warehouse backfill plan --dataset notes --from-year 2016 --to-year 2026 --output data/plans/notes.json
xbrl-warehouse backfill enqueue data/plans/notes.json
xbrl-warehouse worker run
```

## Incremental filings

1. Discover the latest daily XBRL index.
2. Normalize and upsert filing references.
3. Enqueue accessions not already complete for the current extraction revision.
4. Download each filing directory index and the package files Arelle needs (see the
   bronze section of ARCHITECTURE.md). A filing whose index lists only the `.txt`
   submission and `<accession>-xbrl.zip` is expanded from the ZIP.
5. Run Arelle extraction and validation.
6. Write silver Parquet and ClickHouse.
7. Run filing-level reconciliation and quality checks.
8. Mark complete or retry/quarantine.

New filing jobs default to the current extraction revision
(`CURRENT_EXTRACTION_REVISION` in `domain/xbrl.py`, now 3). To re-extract a filing
already loaded at an older revision, enqueue it again; the new revision gets its own
job idempotency key, and older lineage is retained:

```bash
xbrl-warehouse filing enqueue --accession <accession> --cik <cik> \
  --company-name <name> --form <form> --filed-date <YYYY-MM-DD> \
  --primary-document <file>
xbrl-warehouse worker run --job-type ingest_filing
```

## Recovery

### Download interrupted

- Keep `.part` file and byte count.
- Use HTTP range only when the server supports it and the source ETag/last-modified value is unchanged.
- Recompute SHA-256 after completion.

### Worker terminated during parsing

- The job remains locked until the stale-lock timeout.
- The next worker reads stage checkpoints.
- Completed immutable outputs are reused after checksum verification.

### ClickHouse unavailable

- Keep source and Parquet outputs.
- Retry only the warehouse stage.
- Do not re-download or re-parse unless their revision changed.

### Schema drift

- Move the artifact state to `quarantined`.
- Store old/new headers and the source manifest.
- Add a migration/parser update and regression fixture.
- Requeue the same artifact revision after code/schema deployment.

## Backup and rebuild

- PostgreSQL: regular logical or physical backups; it contains operational state and mapping decisions.
- MinIO/object store: versioned replication or filesystem backup; it is the immutable rebuild source.
- ClickHouse: back up for faster recovery, but design so it can be rebuilt from silver Parquet plus PostgreSQL metadata.

## Capacity guardrails

Before a large backfill, check:

- free object-store disk;
- free ClickHouse disk and expected replication overhead;
- temporary directory capacity;
- open-file limits;
- network egress and SEC request rate;
- batch memory under the selected Arrow block size.
