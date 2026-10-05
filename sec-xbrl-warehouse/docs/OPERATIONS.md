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

## Taxonomy packages

Standard taxonomies (us-gaap, srt, dei, and the other SEC families) load from
pinned, unmodified files published by FASB and the SEC, listed in
`config/taxonomy_packages.yml` and kept under `XBRL_ARELLE_TAXONOMY_PACKAGES_DIR`.
Entries have one of three kinds:

- `package`: an XBRL Taxonomy Package; Arelle reads its catalog (FASB us-gaap
  2017+ and srt).
- `archive`: an official zip mapped by explicit URI prefix to a directory inside it
  (us-gaap 2015/2016, the SEC per-family zips, and the SEC combined annual zips
  `https://xbrl.sec.gov/<year>.zip`, mapped per family and year).
- `file`: a schema the SEC publishes only as an individual file (exch 2018,
  stpr 2018, invest 2013).

Every prefix maps under both `http://` and `https://`. `required_prefixes` lists the
family/years the corpus uses; each must be served by an enabled entry.

```bash
xbrl-warehouse taxonomy status   # verify files on disk; no network access
xbrl-warehouse taxonomy sync     # download missing or corrupt enabled packages
```

- `sync` checks every URL first (HTTPS, port 443, no credentials, a host in
  `XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS`). It downloads through the rate-limited client
  with the configured user agent and byte limits, verifies the SHA-256, and only then
  renames the file into place. Verified packages aren't downloaded again.
- Every extraction verifies each enabled package again. A checksum mismatch or an
  unsafe filename stops extraction. A missing package stops it when
  `XBRL_ARELLE_OFFLINE=true` or `XBRL_ENV=production`. Elsewhere, extraction records a
  `warehouse:taxonomyPackageMissing` warning and resolves that taxonomy through the
  allowlisted web cache, so results can depend on the cache until `sync` runs.
- `taxonomy status` and `doctor` report an `uncovered` row for each required prefix
  with no enabled entry. `doctor` fails its "Taxonomy packages" check when any
  enabled entry isn't `ok`, any required prefix is uncovered, or `XBRL_ENV=production`
  and `XBRL_ARELLE_OFFLINE` is false. Offline or production extraction also refuses
  to start while a required prefix is uncovered.
- The W3C `xml.xsd` schemas that the taxonomy package schema imports are shipped with
  the application and never fetched.

### Adding a taxonomy year

1. Find the official package URL on the publisher's site (FASB `xbrl.fasb.org`, SEC
   `xbrl.sec.gov`). The host must be on the allowlist.
2. Download it once and record its SHA-256 and size.
3. If it contains `META-INF/taxonomyPackage.xml` and a catalog limited to that
   family/year, add a `package` entry with its `prefixes`. Otherwise add an `archive`
   entry whose `mappings` point each `https://<host>/<family>/<year>/` prefix at the
   matching directory in the zip (`""` for the zip root). Don't repackage the file.
   Two enabled entries may not claim the same prefix.
4. Add the family/year to `required_prefixes`.
5. Run `taxonomy sync` and `taxonomy status`, then re-extract a filing that uses the
   year offline. Package changes alter extraction input, so they ship with an
   extraction revision bump.

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
