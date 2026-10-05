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

Each filing is two jobs: `fetch_filing` (SEC download, then it enqueues the parse) and
`parse_filing` (Arelle, validation, silver and ClickHouse writes, canonical mapping,
series rebuild; no SEC download). Discovery and `filing enqueue` queue `fetch_filing`
keyed `fetch:<accession>:<revision>`; a completed fetch queues `parse_filing` keyed
`parse:<accession>:<revision>`. Queued legacy `ingest_filing` jobs still run both steps
in one job.

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
xbrl-warehouse worker run --job-type fetch_filing                 # SEC-active
XBRL_WORKER_CONCURRENCY=4 XBRL_ARELLE_OFFLINE=true \
  xbrl-warehouse worker run --job-type parse_filing               # no SEC client
```

A parse job reads the filing's stored manifest (`filings.manifest_object_key`, set by
the fetch). Without one it fails permanently with `ArtifactIntegrityError` ("No stored
package ... run fetch_filing first"); it never downloads. Filings fetched before
migration 0006 have no pointer: enqueue them again (the fetch reuses the cached
manifest and archive, so it makes only the index request).

### Diagnostics per extraction revision

Filing diagnostics carry the revision that raised them, and repeated ones are grouped:

```sql
SELECT extraction_revision, code, severity,
       count(*) AS rows,
       sum(coalesce((details->>'occurrences')::int, 1)) AS occurrences
FROM validation_issues
WHERE accession = '<accession>'
GROUP BY 1, 2, 3
ORDER BY 1 DESC, occurrences DESC;
```

Rows with a NULL `extraction_revision` predate the revision tag.

### Canonical facts after approving mappings

```bash
xbrl-warehouse mapping import-metrics
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text> --dry-run
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text>
xbrl-warehouse mapping remap --epoch <date>
xbrl-warehouse worker run --job-type map_canonical
```

`map_canonical` needs the saved extraction of a completed `parse_filing` (or legacy
`ingest_filing`) job for that revision; filings without one fail with "No saved
extraction" and need re-ingestion.

### Companies

```bash
xbrl-warehouse filing discover --cik 320193   # queue one issuer; the worker stores its company row
xbrl-warehouse worker run --job-type discover_filings
xbrl-warehouse company show AAPL               # or a CIK; reads PostgreSQL only
```

Daily-index discovery (`filing discover --filed-from ... --filed-to ...`) also stores
companies: after a run, the worker fetches the submissions JSON once for each
discovered CIK without a company row, or whose row is older than
`XBRL_COMPANY_REFRESH_DAYS` (default 30), through the same rate-limited client.

The first daily-index run on an empty `companies` table fetches one submissions
document per filer in the range: thousands of requests for a busy week, at the
configured SEC rate (about 10 minutes per 5,000 CIKs at 8 requests/second). Start with
a short range, or seed known issuers with `--cik` first.

`filing discover --cik <cik> --refresh` queues a new job for an issuer already
discovered (a fresh idempotency key), which refetches its submissions and company row.

## Recovery

Dead jobs, retries, and restore drills are in [OPERATIONS.md](OPERATIONS.md#recovery).
A retried job keeps its idempotency key and checkpoints, so it resumes from the last
completed stage. The cases below are specific to ingestion.

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

Backups: [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Capacity checks before a large
backfill: [OPERATIONS.md](OPERATIONS.md#capacity-tuning).
