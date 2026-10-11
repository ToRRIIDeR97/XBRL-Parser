# Engineering Handoff

Latest operational state (3 October 2026): the local warehouse was reset. The Colima Docker disk filled up (a 25 GB uncapped ClickHouse console log plus warehouse data), which crashed PostgreSQL. At the operator's request the PostgreSQL, ClickHouse, and MinIO volumes were deleted and recreated empty; migrations, ClickHouse DDL, and `doctor` pass. The earlier bulk progress (April, May, June, and July 2026 Notes jobs, their checkpoints, and quarantines) and the AAPL 2025 10-K no longer exist locally; the September handover files describe history, not live state. Recreated containers use the Compose log limits. Use `worker run --job-type ingest_filing` to process filing jobs without claiming bulk loads. AAPL (CIK 320193) 10-K/10-Q filings from 2016 to July 2026 are loaded: 42 of 43, with 41,333 facts in the latest views. 26 filings were re-extracted at revision 2 after the taxonomy-encoding fix. The January 2019 10-Q (`0000320193-19-000010`) is missing because SEC's `index.json` for it lists only the `.txt` bundle and `-xbrl.zip`, which the filing downloader can't read yet. Pre-2019 classic-XBRL filings need `filing enqueue --instance-document`. Validation: 183 unit tests and 23 integration tests passed. See the [8 September remediation](docs/history/HARDENING_REMEDIATION_2026-09-08.md) for hardening scope; the API credential split still requires provisioning before deployment.

Storage, speed, and security audit (8 October 2026, branch
`audit/speed-security-storage`): applied to the local stack. ClickHouse dropped the
stale `system.*_0` log tables, the 2026-10-05 backup database and `__backup_` tables
(verified against the live tables first), and `trace_log`; server logs now rotate at
50 MB x 3. PostgreSQL dropped the three 2026-10-05 backup/scratch databases (same row
counts as live), is at migration `0007_drop_prefix_indexes`, runs with WAL
compression, and its control database went from 225 MB to 62 MB: 162,803 legacy
validation issues were grouped into 42,563 rows (occurrence totals unchanged) and the
36,452 superseded `mapping_reviews` were deleted (dump in
`data/backups/xbrl_control_validation_issues_mapping_reviews_20261008.sql.gz`; this
makes the 0003 downgrade unable to reopen them). `storage recompress-extractions`
rewrote the 68 plain-JSON saved extractions (906 MB) as zstd (45 MB) and deleted the
plain copies; the bucket is now versioned, so MinIO keeps them as noncurrent versions
until they expire after 30 days. `storage retire-revisions` removed the revision-1
rows of the 26 filings re-extracted at revision 2 (`v_xbrl_facts_latest` still has
41,333 facts). `db upgrade-clickhouse --yes` rebuilt `canonical_facts` for its new sort
key (empty table; its backup was dropped), `db init-clickhouse` applied ZSTD(3) and the
accession bloom indexes, and the indexes were materialized. The least-privilege
identities (`xbrl_worker`, `xbrl_writer`, `xbrl_api` and its profile, the MinIO
`xbrl_worker` user) are provisioned with the development passwords. The application
images weren't rebuilt or started in this change. Unit and integration suites pass.

Pending after the filing package selection, dei fiscal-field, and SEC inline
transform changes (extraction revision 3, selection policy 2): re-extract the 42 loaded AAPL filings and ingest
`0000320193-19-000010` from its `-xbrl.zip`, then compare member counts,
`xmlSchema:syntax` counts, and fact counts against revision 2. Confirm
`SELECT countIf(fiscal_year IS NULL) FROM xbrl.v_xbrl_facts_latest` returns 0. Confirm
`SELECT count() FROM xbrl.v_xbrl_facts_latest WHERE value_text = '(ixTransformValueError)'`
returns 0. Revision 3 also fixes fact `taxonomy_version` (it was `2001` for every fact). Then run
`mapping import-metrics`, `mapping approve-seeds` (dry run first), `mapping remap`, and
`worker run --job-type map_canonical`, and confirm AAPL canonical facts. Run `db upgrade-clickhouse --yes` for the new `accepted_at` and
filing-context columns, then check AAPL operating cash flow FY2016-FY2025 in
`GET /v1/series/320193?metrics=operating_cash_flow` (four quarters sum to FY). Use the per-revision diagnostics query in INGESTION_RUNBOOK.md to confirm
which issues belong to the live revision. Before that, run
`xbrl-warehouse taxonomy sync` (50 pinned FASB and SEC sources; `taxonomy status` must
show no `uncovered` rows), set
`XBRL_ARELLE_OFFLINE=true` for the re-extraction, and confirm no `webCache:*` issues.
Until then the live data stays at revisions 1 and 2.

Plans 12-15 are applied locally (Alembic at 0007, ClickHouse upgraded, identities
provisioned with `ops/provision/provision_identities.sh`, which also grants
`xbrl_api`). Filings are now
`fetch_filing` → `parse_filing` jobs: run one SEC-active worker with
`--job-type fetch_filing --job-type discover_filings` and a parse worker with
`XBRL_ARELLE_OFFLINE=true XBRL_WORKER_CONCURRENCY=N --job-type parse_filing` (compose
profile `parse`); give the SEC-active worker `XBRL_WORKER_CONCURRENCY=4` too. Rebuild
the images (`docker compose build api worker admin`) before starting them. Then benchmark the AAPL re-ingest against the 43-minute serial
baseline (OPERATIONS.md, capacity tuning).

Plan 17 (source-neutral filing and entity keys) adds Alembic
`0008_filing_entity_keys`, not yet applied to the live control database: back up
PostgreSQL (BACKUP_RESTORE.md), run `alembic upgrade head` through the `admin`
service, then `doctor`. ClickHouse and existing SEC rows and object keys are unchanged.

Plan 18 (source adapters) routes discovery, fetch, and parse fiscal fields through a
`SourceRegistry` built from `XBRL_ENABLED_SOURCES` (default `sec`). SEC requests, user
agent, rate, lock name, object keys, and job keys are unchanged, and there's no
migration. Only the `sec` adapter exists; enabling another code stops `worker run` at
startup until its plan lands (OPERATIONS.md, "Sources and per-source egress").

## Current state

Implementation phases A–F are complete. The repository contains working historical and incremental ingestion, Arelle extraction, canonical mapping, replay-safe storage, bounded query APIs, worker recovery, metrics, and operational procedures. `FUNCTION_STUBS.md` reports zero implementation markers.

No live SEC production corpus or historical backfill was run while completing the implementation. Those require the operator's identifying user agent, network access, storage budget, and explicit production scope.

The nine ranked performance/security audit findings have code fixes and regression coverage. The [completion record](docs/history/AUDIT_IMPLEMENTATION_PLAN_2026-09-07.md) has the validation evidence; current query budgets and reader requirements are in [SECURITY.md](docs/SECURITY.md). Configure the production API key and reader profile before deployment; recreate services to apply the new Compose memory limits.

## Verified locally

The [launch hardening follow-up](docs/history/LAUNCH_HARDENING_2026-09-07.md) covers
additional checkpoint fencing, bounded cache/taxonomy reads and reconciliation,
mandatory raw retention, API readiness protection, and container changes.
The local storage containers have been recreated with loopback-only ports.
Existing application volumes need UID/GID `10001:10001` ownership before upgrade;
`XBRL_RETAIN_FILING_SOURCE_FILES=false` is now rejected.

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
./ops/provision/provision_identities.sh
xbrl-warehouse taxonomy sync
xbrl-warehouse doctor
```

`taxonomy sync` installs the pinned taxonomy packages from `config/taxonomy_packages.yml`; `doctor` fails while an enabled package is missing. See OPERATIONS.md, "Taxonomy packages".

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
xbrl-warehouse filing discover --cik 320193   # one issuer, stores its company row
xbrl-warehouse company show AAPL
xbrl-warehouse worker run --job-type fetch_filing --job-type discover_filings
XBRL_ARELLE_OFFLINE=true XBRL_WORKER_CONCURRENCY=4 xbrl-warehouse worker run --job-type parse_filing
```

The overlapping discovery range is deliberate and idempotent. Schedule only the discovery command; long-running workers consume the resulting PostgreSQL jobs.

## Canonical workflow

```bash
xbrl-warehouse mapping import-metrics
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text> --dry-run
xbrl-warehouse mapping approve-seeds --reviewer <name> --rationale <text>
xbrl-warehouse mapping remap --epoch <date>
xbrl-warehouse worker run --job-type map_canonical
xbrl-warehouse mapping reviews
xbrl-warehouse mapping approve --help
```

Only approved, versioned mappings produce canonical facts. Raw facts and their source/mapping lineage remain queryable.

## Upgrade and recovery

- Run `doctor` before migrations or workers.
- Only one process can send SEC traffic at a time; a second one fails fast on the SEC egress lock (docs/SEC_ACCESS.md). Parse-only workers (`--job-type parse_filing`, offline Arelle) build no SEC client and can run beside it.
- Inspect and revive dead jobs with `xbrl-warehouse jobs list --state dead` and `jobs retry <id>`.
- Review `docs/BACKUP_RESTORE.md`, take backups, and verify free space.
- Apply Alembic, then `xbrl-warehouse db init-clickhouse` for compatible DDL.
- If `doctor` reports incompatible ClickHouse sort keys, run `xbrl-warehouse db upgrade-clickhouse --yes`; old tables remain as timestamped backups.
- Increment `CURRENT_EXTRACTION_REVISION` (`src/sec_xbrl_warehouse/domain/xbrl.py`) for parser changes; `filing enqueue` and filing discovery use it as the default. Never overwrite prior package bytes or extraction lineage.
- Rebuild ClickHouse from immutable Parquet/object storage plus PostgreSQL control state when required.

## Production acceptance still required

Before calling the first production backfill complete:

1. Run the real-filing matrix in `docs/TEST_PLAN.md`, including inline XBRL, IFRS, dimensions, amendments, duplicates, and invalid filings.
2. Perform a restore drill in the target PostgreSQL/object-store/ClickHouse infrastructure and record RPO/RTO results.
3. Canary the most recent bulk period, then complete and reconcile the 2016-to-current backfill.
4. Independently parse and compare at least 100 sampled filings.
5. Add deployment-specific TLS, authentication, managed secrets, network policy, replication, and alerts.

These are external data and deployment gates; there are no remaining placeholder implementation functions.
