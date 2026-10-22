# Backup and Restore

## Backup scope

- **Object storage:** bronze package archives (and legacy loose source files), manifests, quarantine diagnostics, zstd extraction checkpoints (`.json.zst`, plus legacy `.json`), and silver Parquet. This is the durable rebuild source. A filing's raw bytes live in its package archive; verify restored archives against `package_checksum_sha256` and member checksums in the manifests.
- **PostgreSQL:** artifacts, filings, jobs/checkpoints, validation issues, mapping definitions/reviews, and run metadata.
- **ClickHouse:** optional snapshots for faster recovery; not the sole copy of raw data.
- **Configuration/code:** Git tag, dependency lock, SQL migration versions, and Arelle/parser version.

With source retention explicitly disabled, successful filing ZIPs may already be
retired. Keep extraction snapshots, silver Parquet and manifests in backups: they
preserve extracted facts and mapping replay, but cannot reconstruct every original
source byte or rerun a different parser. Restore a retired ZIP's prior MinIO version
within the 30-day noncurrent retention window, or reacquire it from its permitted
source. Re-enabling retention does not restore previously deleted objects.

## Backup procedure

1. Pause new producers.
2. Record a run watermark and active job list.
3. Back up PostgreSQL with `pg_dump --format=custom`; record its SHA-256 and the Alembic revision.
4. Replicate/version object storage to another failure domain without deleting the destination's older versions.
5. Optionally create ClickHouse backups.
6. Store a manifest with checksums, object count/bytes, schema revisions, Git commit, dependency lock, ClickHouse version, and Arelle version.

Example control-plane backup (supply credentials through the environment or a password file, not the command line):

```bash
install -d -m 0700 /srv/backups/xbrl/2026-09-04
pg_dump --dbname=postgresql://xbrl@127.0.0.1:5432/xbrl_control \
  --format=custom --no-owner \
  --file=/srv/backups/xbrl/2026-09-04/control.dump
sha256sum /srv/backups/xbrl/2026-09-04/control.dump
mc mirror production/sec-xbrl replica/sec-xbrl/2026-09-04
```

Use the storage provider's versioned replication instead of `mc mirror` when available. Never use a mirror option that deletes destination objects.

## Restore procedure

1. Create new, empty PostgreSQL/ClickHouse databases and a new object-store bucket. Never test a restore over live names.
2. Verify backup-manifest and object checksums before reading them.
3. Restore PostgreSQL and object storage into those isolated names.
4. Deploy the exact recorded code/dependency versions and apply migrations only up to the recorded revisions.
5. Restore a ClickHouse snapshot or recreate schemas and replay silver Parquet.
6. Run `doctor`, then reconcile artifact/accession counts, checksums, sampled raw facts, canonical lineage, and dead/running jobs.
7. Reclaim only jobs whose lock is stale and whose checkpoint inputs still match.

Example isolated PostgreSQL validation:

```bash
createdb --host=127.0.0.1 --username=xbrl xbrl_restore_drill_20260904
pg_restore --host=127.0.0.1 --username=xbrl \
  --dbname=xbrl_restore_drill_20260904 --exit-on-error --no-owner \
  /srv/backups/xbrl/2026-09-04/control.dump
psql --host=127.0.0.1 --username=xbrl --dbname=xbrl_restore_drill_20260904 \
  --command='SELECT version_num FROM alembic_version;'
```

Set all application DSNs/database/bucket names to the drill targets before running `doctor`; stop immediately if any resolves to production.

## Required drills

Run quarterly restore drills against a clean environment. Record start/end time, recovery point, checksum/object/row comparisons, failed steps, and cleanup. A backup is not accepted until representative source artifacts, Arelle bundles, canonical facts, mapping reviews, checkpoints, and lineage queries are recovered and reconciled.
