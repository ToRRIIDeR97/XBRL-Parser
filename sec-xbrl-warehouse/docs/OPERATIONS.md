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
- Job queue has no unexplained `dead` growth (`xbrl-warehouse jobs list --state dead`).
- SEC 429/5xx and retry counts remain low.
- Bronze object count and PostgreSQL artifact count reconcile.
- ClickHouse disk free space exceeds the configured safety threshold.
- Quarantine and mapping-review queues are triaged.

## SEC egress lock

Only one process sends SEC traffic at a time (see SEC_ACCESS.md). If `catalog sync`,
`taxonomy sync`, or a second worker reports "another process holds the SEC egress
lock", wait for the SEC-active worker to finish or stop it. Workers scoped to non-SEC
jobs aren't affected. A worker that hits the lock defers the job by
`XBRL_SEC_EGRESS_BUSY_RETRY_SECONDS` without spending an attempt and without recording
validation issues. Deferral is capped: after `XBRL_SEC_EGRESS_MAX_DEFER_SECONDS`
(default 6 hours) from the first deferral the job goes `dead` with that reason
(`xbrl_job_dead_letters_total{reason="egress_busy_timeout"}`); `XbrlSecEgressDeferrals`
fires after about 30 minutes of continued deferrals (15-minute window held 15 minutes).

A worker keeps the lock for its whole lifetime once it has sent an SEC request, so
`catalog sync` and `taxonomy sync` fail as busy until that worker stops. The holder
checks its lock connection at most every `XBRL_SEC_EGRESS_CHECK_SECONDS` (default 30)
before SEC requests; if the connection dropped (which releases the lock), it
re-acquires the lock or stops with `SecEgressBusyError` instead of sending.

## Fetch and parse workers

`fetch_filing`, `ingest_filing`, `discover_filings`, and `download_bulk` send SEC
traffic (`requires_sec`). A worker builds the SEC client and egress lock only when its
`--job-type` set includes one of them (or has no filter). A worker scoped to
`parse_filing` (and other non-SEC types) builds no SEC client, so it can run beside the
SEC-active worker:

```bash
xbrl-warehouse worker run --job-type fetch_filing --job-type discover_filings
XBRL_WORKER_CONCURRENCY=4 xbrl-warehouse worker run --job-type parse_filing
```

- **Offline Arelle (recommended for parse workers).** With `XBRL_ARELLE_OFFLINE=true`
  a parse worker never touches the SEC or the egress lock. In online mode Arelle may
  resolve taxonomies from SEC hosts, so a parse worker takes the egress lock lazily
  before its first extraction and holds it until it stops, exactly like a SEC-active
  worker; a second online parse worker then defers its jobs as busy.
- **Concurrency.** `XBRL_WORKER_CONCURRENCY` (default 1, at most 32) is how many jobs
  one worker runs at once. It claims a job only when a slot is free; each job has its
  own lease, heartbeat, and checkpoint ownership (`FOR UPDATE SKIP LOCKED` still
  prevents double claims). Each running extraction is an Arelle child process, so
  `XBRL_WORKER_CONCURRENCY x XBRL_ARELLE_MEMORY_LIMIT_BYTES` must fit the container
  memory limit; `doctor` reports "Worker memory" and fails when it doesn't. On stop, a
  worker lets its running jobs finish.
- Compose: `docker compose --profile parse up -d worker-parse` starts an offline
  parse-only worker (`XBRL_PARSE_WORKER_CONCURRENCY`, default 2;
  `WORKER_PARSE_MEMORY_LIMIT`, default 8g).

## Job queue

- **Retries.** A failed job is retried when the error is transient: network and
  timeout errors, HTTP 403 (SEC throttling), 408, 429 and 5xx responses (other 4xx
  are permanent), ClickHouse connection errors and server codes listed in
  `TRANSIENT_CLICKHOUSE_CODES` (for example 252 `TOO_MANY_PARTS`, 241
  `MEMORY_LIMIT_EXCEEDED`, 159 `TIMEOUT_EXCEEDED`), and S3/MinIO connection errors,
  timeouts, and codes such as `SlowDown`, `ServiceUnavailable`, `InternalError`,
  `RequestTimeout`, or HTTP 500/503. A wrapped error is classified by its cause.
  Integrity and schema-drift errors, ClickHouse syntax or missing-table errors, and
  S3 `AccessDenied`/`NoSuchBucket` stay permanent.
- **Backoff.** `available_at` is database time plus 2^attempts seconds with 50-100%
  jitter, capped at one hour; worker clocks don't affect it.
- **Dead-lettering.** A job goes `dead` on a permanent error, after its last retry,
  or when its lease expired on its last attempt (for example the process crashed).
  The claim that finds the latter records the reason in `last_error` and claims the
  next job in the same call. A worker started with `--job-type` never claims or
  dead-letters other types.
- **Recovery.**

  ```bash
  xbrl-warehouse jobs list --state dead [--type parse_filing]
  xbrl-warehouse jobs retry <job-id>   # dead -> retry, attempts reset, claimable now
  ```

  `retry` keeps the previous error in `last_error` and refuses jobs that aren't dead.
- **Metrics.** The worker metrics endpoint exports, at scrape time,
  `xbrl_queue_jobs{job_type,state}`, `xbrl_queue_oldest_ready_age_seconds`, and
  `xbrl_queue_snapshot_up`, plus counters `xbrl_job_transitions_total` and
  `xbrl_job_dead_letters_total{job_type,reason}`. `ops/alerts.yml` alerts on dead
  jobs, new dead letters, a ready job older than an hour, and a failing snapshot.

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
- `taxonomy status` reports `mapping_missing` for an `archive` entry whose mapped
  directory has no files in the zip, which also stops extraction.
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

## Object-store inventory

`xbrl-warehouse storage report [--keys-file PATH]` (run through the `worker`
service) lists every object and prints JSON: objects and bytes per kind (package
archives, legacy loose members, manifests, `.json` and `.json.zst` extraction
checkpoints, silver Parquet, quarantine) and the reclaimable legacy objects:

- `loose_members_in_archives`: loose bronze members that a stored manifest records
  inside an existing package archive (materialization reads the archive);
- `json_checkpoints_with_zst`: uncompressed extraction checkpoints whose accession
  and revision also have a `.json.zst` checkpoint.

It only lists; nothing is deleted or written. `--keys-file` writes the reclaimable
keys locally, one per line. Deleting them is a separate, operator-approved retention
decision. Completed jobs and `mapping remap` read the checkpoint key their job
recorded, so before deleting a JSON checkpoint confirm that the latest completed job
for that accession and revision points at the `.json.zst` key (re-ingest otherwise).

## Safe pause

Stop producers first, then wait for active jobs to checkpoint. Stop workers only after heartbeats cease. PostgreSQL and object storage are the source of recovery state; ClickHouse can be rebuilt from silver Parquet.

## Incident categories

- Source/access: reduce concurrency/rate, inspect identity and SEC status, preserve retries.
- Schema drift: quarantine only the changed artifact, compare SEC documentation and captured header.
- Parser regression: pin Arelle version, retain package/log, rerun corpus.
- Partial warehouse write: replay the same artifact/accession revision and reconcile latest-state counts.
- Mapping error: create a new mapping version; never mutate historical mapping lineage in place.

## Capacity tuning

Benchmark (operator follow-up): re-ingest the AAPL filings with one fetch worker and
one parse worker at concurrency 4; compare `xbrl_stage_latency_seconds` per stage and
the total wall time with the 43-minute serial baseline (2 October 2026).

1. Start with one worker and one recent artifact while measuring RSS, temporary disk, object throughput, ClickHouse insert latency, and SEC throttle time.
2. Set `XBRL_ARROW_BATCH_ROWS` so one batch plus conversion overhead fits comfortably inside the worker memory limit; 50,000 is only a starting point.
3. Reserve space for bronze bytes, silver Parquet, ClickHouse data/merges, and one preserving schema copy before a backfill.
4. Increase workers one at a time. Stop when SEC throttling, PostgreSQL claims, object throughput, Arelle memory, or ClickHouse latency becomes the bottleneck.
5. Keep the aggregate SEC request ceiling at or below policy. The limiter is process-local; the SEC egress lock lets only one process sharing this control database send SEC traffic, so extra worker replicas can only run non-SEC jobs.

Before a preserving ClickHouse upgrade, `doctor` must pass and free ClickHouse space must exceed 110% of the affected table bytes. The upgrade command refuses insufficient capacity and retains named backup tables.

### ClickHouse memory

ClickHouse caps its total memory at 0.9 × the container limit (3.6 GiB with the
default `CLICKHOUSE_MEMORY_LIMIT=4g`). Every query, background merge and cache counts
against that one cap, and when it's reached ClickHouse stops whichever query it picks
(`MEMORY_LIMIT_EXCEEDED`, code 241), often an unrelated small one.
`docker/clickhouse/config.d/xbrl-memory.xml`, mounted by Compose, keeps the server
inside its limit:

- `system.metric_log` and `system.query_metric_log` are off. They have about a
  thousand columns, and each background merge of `metric_log` took 3.1 GiB for about
  a second, killing whatever query ran at that moment. The warehouse exports its own
  Prometheus metrics instead.
- Other system logs expire (trace and processor logs after 3 days, text and
  asynchronous-metric logs after 7, query and part logs after 14).
- Caches are capped at 128–256 MiB each; the defaults (5–8 GiB) exceed the container.

When a container that predates this file is recreated, ClickHouse keeps the old log
tables as `system.<log>_0` without expiry, and the old `metric_log` and
`query_metric_log` still merge. Detach them (`DETACH TABLE system.metric_log
PERMANENTLY`, same for `query_metric_log`); the `_0` tables can be dropped when their
history is no longer needed.

To diagnose a code-241 failure, check what else held memory at that second:
`system.text_log` lines from `MemoryTracker` ("Background process (mutate/merge) peak
memory usage") and `system.query_log` `memory_usage` for queries in the same minute.

## Production boundary

The Compose profile binds dashboards and the API to localhost, but production still needs authentication, TLS, managed secrets, network policy, backup monitoring, object replication/lifecycle rules, pinned image digests, and routed alerts.
