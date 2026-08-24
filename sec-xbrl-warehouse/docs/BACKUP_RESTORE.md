# Backup and Restore

## Backup scope

- **Object storage:** bronze source files, manifests, quarantine diagnostics, and silver Parquet. This is the durable rebuild source.
- **PostgreSQL:** artifacts, filings, jobs/checkpoints, validation issues, mapping definitions/reviews, and run metadata.
- **ClickHouse:** optional snapshots for faster recovery; not the sole copy of raw data.
- **Configuration/code:** Git tag, dependency lock, SQL migration versions, and Arelle/parser version.

## Backup procedure

1. Pause new producers.
2. Record a run watermark and active job list.
3. Back up PostgreSQL with a consistent snapshot.
4. Replicate/version object storage to another failure domain.
5. Optionally create ClickHouse backups.
6. Store a manifest with checksums, schema revisions, and software versions.

## Restore procedure

1. Restore PostgreSQL and object storage.
2. Deploy the exact recorded code/dependency versions.
3. Apply migrations only up to the recorded versions.
4. Restore ClickHouse snapshot or recreate schemas and replay silver Parquet.
5. Reconcile artifact/accession counts and hashes before resuming workers.
6. Reclaim only jobs whose lock is stale and whose checkpoint inputs still match.

## Required drills

Run quarterly restore drills against a clean environment. A backup is not accepted until representative source artifacts, Arelle bundles, canonical facts, and lineage queries are recovered and reconciled.
