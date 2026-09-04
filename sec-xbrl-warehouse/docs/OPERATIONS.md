# Operations Runbook

## Service order

1. PostgreSQL
2. ClickHouse
3. MinIO/object storage
4. Alembic migration
5. ClickHouse DDL
6. worker
7. API

## Routine checks

- `/health/ready` is green.
- Job queue has no unexplained `dead` growth.
- SEC 429/5xx and retry counts remain low.
- Bronze object count and PostgreSQL artifact count reconcile.
- ClickHouse disk free space exceeds the configured safety threshold.
- Quarantine and mapping-review queues are triaged.

## Metrics and dashboard

Start the local monitoring profile with:

```bash
make monitoring
```

Prometheus is available at `http://127.0.0.1:9090` and Grafana at `http://127.0.0.1:3000` unless their host ports are overridden. Grafana provisions the **SEC XBRL Warehouse** dashboard from `ops/grafana/dashboards/xbrl-warehouse.json`.

The API exposes process metrics at `/metrics`; the worker exposes `XBRL_WORKER_METRICS_HOST:XBRL_WORKER_METRICS_PORT`. Keep both endpoints private. Alert at minimum on dead/lease-lost jobs, sustained SEC 429/5xx responses, schema drift, validation-error growth, reconciliation differences, and a missing scrape target.

## Scheduled discovery

Use the host's native scheduler. The rolling scan deliberately overlaps completed UTC days; PostgreSQL idempotency prevents duplicate filing jobs.

Example cron entry:

```cron
15 03 * * * cd /srv/sec-xbrl-warehouse && .venv/bin/xbrl-warehouse filing discover-latest --lookback-days 3 >> /var/log/xbrl-discovery.log 2>&1
```

Run workers as supervised long-lived services. Do not schedule parallel worker launches or pass `--refresh` in a routine schedule.

## Safe pause

Stop producers first, then wait for active jobs to checkpoint. Stop workers only after heartbeats cease. PostgreSQL and object storage are the source of recovery state; ClickHouse can be rebuilt from silver Parquet.

## Incident categories

- Source/access: reduce concurrency/rate, inspect identity and SEC status, preserve retries.
- Schema drift: quarantine only the changed artifact, compare SEC documentation and captured header.
- Parser regression: pin Arelle version, retain package/log, rerun corpus.
- Partial warehouse write: replay the same artifact/accession revision and reconcile latest-state counts.
- Mapping error: create a new mapping version; never mutate historical mapping lineage in place.

## Capacity tuning

1. Start with one worker and one recent artifact while measuring RSS, temporary disk, object throughput, ClickHouse insert latency, and SEC throttle time.
2. Set `XBRL_ARROW_BATCH_ROWS` so one batch plus conversion overhead fits comfortably inside the worker memory limit; 50,000 is only a starting point.
3. Reserve space for bronze bytes, silver Parquet, ClickHouse data/merges, and one preserving schema copy before a backfill.
4. Increase workers one at a time. Stop when SEC throttling, PostgreSQL claims, object throughput, Arelle memory, or ClickHouse latency becomes the bottleneck.
5. Keep the aggregate SEC request ceiling at or below policy across every replica; the supplied limiter is process-local.

Before a preserving ClickHouse upgrade, `doctor` must pass and free ClickHouse space must exceed 110% of the affected table bytes. The upgrade command refuses insufficient capacity and retains named backup tables.

## Production boundary

The Compose profile binds dashboards and the API to localhost, but production still needs authentication, TLS, managed secrets, network policy, backup monitoring, object replication/lifecycle rules, pinned image digests, and routed alerts.
