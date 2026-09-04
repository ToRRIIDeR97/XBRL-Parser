# Engineering Handoff

## Current state

Implementation phases A–F are complete. The repository contains working historical and incremental ingestion, Arelle extraction, canonical mapping, replay-safe storage, bounded query APIs, worker recovery, metrics, and operational procedures. `FUNCTION_STUBS.md` reports zero implementation markers.

No live SEC production corpus or historical backfill was run while completing the implementation. Those require the operator's identifying user agent, network access, storage budget, and explicit production scope.

## Verified locally

Run the same gates before release:

```bash
python -m compileall src tests scripts
ruff check .
ruff format --check .
mypy src
pytest -m "not integration" --cov
RUN_XBRL_INTEGRATION=1 pytest -m integration
python scripts/scan_stubs.py
```

Integration fixtures create uniquely named PostgreSQL and ClickHouse databases plus a MinIO bucket, then remove them after the run. They do not truncate the configured application databases.

## Bring-up

```bash
cp .env.example .env
# Replace XBRL_SEC_USER_AGENT before any SEC request.
docker compose up -d postgres clickhouse minio minio-init
python -m pip install -e ".[dev]"
alembic upgrade head
xbrl-warehouse db init-clickhouse
xbrl-warehouse doctor
```

Use `make monitoring` to add the API, worker, Prometheus, and Grafana. Host-facing ports bind to localhost by default. The API exposes `/metrics`; the worker exposes its configured metrics port inside the Compose network.

## Historical path

```bash
xbrl-warehouse catalog sync --dataset notes
xbrl-warehouse backfill plan --dataset notes --from-year 2016 --to-year 2026 --output data/plans/notes.json
xbrl-warehouse backfill enqueue data/plans/notes.json
xbrl-warehouse worker run
```

Canary one artifact before enqueuing the full range. Source ZIPs, Parquet, stage checkpoints, and warehouse rows are revisioned so replay resumes from the latest valid checkpoint.

## Incremental path

```bash
xbrl-warehouse filing discover-latest --lookback-days 3
xbrl-warehouse worker run
```

The overlapping discovery range is deliberate and idempotent. Schedule only the discovery command; long-running workers consume the resulting PostgreSQL jobs.

## Canonical workflow

```bash
xbrl-warehouse mapping import-metrics
xbrl-warehouse mapping reviews
xbrl-warehouse mapping approve --help
```

Only approved, versioned mappings produce canonical facts. Raw facts and their source/mapping lineage remain queryable.

## Upgrade and recovery

- Run `doctor` before migrations or workers.
- Review `docs/BACKUP_RESTORE.md`, take backups, and verify free space.
- Apply Alembic, then `xbrl-warehouse db init-clickhouse` for compatible DDL.
- If `doctor` reports incompatible ClickHouse sort keys, run `xbrl-warehouse db upgrade-clickhouse --yes`; old tables remain as timestamped backups.
- Increment `extraction_revision` for parser changes. Never overwrite prior package bytes or extraction lineage.
- Rebuild ClickHouse from immutable Parquet/object storage plus PostgreSQL control state when required.

## Production acceptance still required

Before calling the first production backfill complete:

1. Run the real-filing matrix in `docs/TEST_PLAN.md`, including inline XBRL, IFRS, dimensions, amendments, duplicates, and invalid filings.
2. Perform a restore drill in the target PostgreSQL/object-store/ClickHouse infrastructure and record RPO/RTO results.
3. Canary the most recent bulk period, then complete and reconcile the 2016-to-current backfill.
4. Independently parse and compare at least 100 sampled filings.
5. Add deployment-specific TLS, authentication, managed secrets, network policy, replication, and alerts.

These are external data and deployment gates; there are no remaining placeholder implementation functions.
