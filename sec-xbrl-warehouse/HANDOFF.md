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

Revision 3 re-extraction and canonical facts (16 October 2026, branch
`ops/execute-handoff-backlog`): applied to the local stack from the host virtualenv.
PostgreSQL was backed up (`data/backups/2026-10-16/control.dump`, SHA-256 in the
`.sha256` file beside it, taken at 0007) and upgraded to `0008_filing_entity_keys`.
`taxonomy sync` installed the 50 pinned packages; `taxonomy status` has no
`uncovered` rows and `doctor` passes. `filing discover --cik 320193 --forms 10-K,10-Q
--filed-from 2016-01-01` queued all 43 AAPL filings at revision 3, and all 43 are now
at revision 3, including `0000320193-19-000010` (839 facts). The other 42 filings
have the same fact counts as at revisions 1 and 2. `v_xbrl_facts_latest` has 42,172
facts, none with a NULL `fiscal_year` or an `(ixTransformValueError)` value, with 14
distinct `taxonomy_version` values. Revision 3 raised no `webCache:*` issues and 1,300
`xmlSchema:syntax` occurrences. Two bugs were fixed on the way:

- The 13 classic (non-inline) filings from 2016 to January 2019 parsed their HTML
  primary document and failed with "Arelle extracted no facts". The parse entrypoint
  for a filing with `is_inline_xbrl` false and no `instance_document` is now its
  instance XML (ARCHITECTURE.md, bronze section), so `--instance-document` is no
  longer needed for them. The failed attempts had saved an empty `extract`
  checkpoint, which a plain `jobs retry` replays, so their `extract` and `validation`
  checkpoints were deleted before the retry. Their first-attempt diagnostics (one
  `xmlSchema:syntax`, one `exception:RecursionError` and the `XbrlExtractionError`
  rows per filing) remain in `validation_issues` at revision 3.
- Concurrent `map_canonical` jobs deadlocked on `mapping_reviews` upserts, and the
  deadlock was classified as permanent. Review rows are now upserted in concept
  order, and a SQLAlchemy error whose driver-level cause is unclassified is judged by
  its message, so deadlocks retry.

`mapping import-metrics` loaded 46 metrics. `mapping approve-seeds` (dry run first)
approved 109 seed mappings with reviewer `ToRRIIDeR`, and `mapping remap --epoch
2026-10-16` plus `map_canonical` produced 12,361 canonical facts over 40 metrics for
all 43 filings. ClickHouse already had `accepted_at` and the filing-context columns
(`doctor` reports compatible sort keys), so `db upgrade-clickhouse` wasn't needed.
`GET /v1/series/320193?metrics=operating_cash_flow` returns FY2016-FY2025 with the
four quarters summing exactly to each fiscal year (FY2025 111,482 and FY2024 118,254
USD millions).

Benchmark against the 43-minute serial baseline: with one SEC-active worker
(`--job-type discover_filings --job-type fetch_filing`, concurrency 4) and an offline
parse worker (concurrency 4, its own `XBRL_WORKER_METRICS_PORT`), discovery, all 43
fetches, and the 30 inline parses finished in 2 minutes 12 seconds; the 13 classic
parses took under 2 minutes after the fix. Two workers on one host need different
`XBRL_WORKER_METRICS_PORT` values, or the second fails with "Address already in use".
The `api`, `worker` and `admin` images were rebuilt from this branch.

Plans 12-15 are applied locally (identities provisioned with
`ops/provision/provision_identities.sh`, which also grants `xbrl_api`). Filings are
`fetch_filing` → `parse_filing` jobs: run one SEC-active worker with
`--job-type fetch_filing --job-type discover_filings` and a parse worker with
`XBRL_ARELLE_OFFLINE=true XBRL_WORKER_CONCURRENCY=N --job-type parse_filing` (compose
profile `parse`); give the SEC-active worker `XBRL_WORKER_CONCURRENCY=4` too.

Plan 17 (source-neutral filing and entity keys) adds Alembic
`0008_filing_entity_keys`, applied to the live control database on 16 October 2026.
ClickHouse and existing SEC rows and object keys are unchanged.

Plan 18 (source adapters) routes discovery, fetch, and parse fiscal fields through a
`SourceRegistry` built from `XBRL_ENABLED_SOURCES` (default `sec`). SEC requests, user
agent, rate, lock name, object keys, and job keys are unchanged, and there's no
migration. Only the `sec` adapter exists; enabling another code stops `worker run` at
startup until its plan lands (OPERATIONS.md, "Sources and per-source egress").

Plan 19 adds the `esef` adapter (EU/UK ESEF reports from filings.xbrl.org), off by
default, with no migration. It isn't enabled anywhere yet. Before enabling it the
operator confirms filings.xbrl.org's reuse terms, pins the ESEF and IFRS taxonomy
packages (`source_required_prefixes.esef` is listed but has no pinned entries, so
`taxonomy status` shows `uncovered` rows and `doctor` fails once `esef` is enabled),
adds `www.esma.europa.eu,www.ifrs.org` to `XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS` in
`.env`, and runs the AC-6 canary (OPERATIONS.md, "ESEF filings").

Plan 20 adds the `edinet` (Japan, FSA EDINET API v2) and `dart` (Korea, OpenDART)
adapters, off by default, with no migration. Neither is enabled anywhere yet. Before
enabling them the operator registers their own API keys (`XBRL_EDINET_API_KEY`,
`XBRL_DART_API_KEY` in `.env`, never shared), pins the EDINET and DART taxonomy
packages (`source_required_prefixes.edinet` and `.dart` are listed without pinned
entries, so `taxonomy status` and `doctor` fail once either is enabled; the DART
prefix is unverified), adds
`disclosure.edinet-fsa.go.jp,disclosure2dl.edinet-fsa.go.jp` to
`XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS` in `.env`, and runs the AC-9 canary
(OPERATIONS.md, "EDINET and DART filings").

Plan 21 adds an operator file inbox with the `nse` (NSE/BSE results, ISIN) and `mca`
(MCA21 AOC-4 XBRL, CIN) profiles, off by default, with no migration and no network
access: the operator obtains files under each source's terms and places them with an
`index.csv` in `XBRL_INBOX_DIR/<code>/` (default `data/inbox`). Neither is enabled
anywhere yet. Before enabling them the operator pins the NSE/BSE and MCA taxonomy
packages (`source_required_prefixes.nse` and `.mca` are listed, unverified, without
pinned entries, so `taxonomy status` and `doctor` fail once either is enabled),
runs `inbox check --source <code>`, and runs the AC-7 canary (OPERATIONS.md,
"Operator file inbox").

Plan 22 adds the `acra` inbox profile for Singapore ACRA XBRL financial statements
(entity key from the UEN, scheme `https://www.acra.gov.sg/uen`), off by default, with
no migration and no network access. ACRA has no public retrieval API; the warehouse
never automates BizFile+ and holds no Singpass or ACRA credentials. Before enabling
`acra` outside local testing the operator records how files are obtained (company
officers, a licensed vendor) and confirms redistribution terms, pins the ACRA
taxonomy (`source_required_prefixes.acra` lists the unverified
`www.acra.gov.sg/taxonomy/` without a pinned entry, so `taxonomy status` and `doctor`
fail once `acra` is enabled), runs `inbox check --source acra`, and runs a three-file
canary (OPERATIONS.md, "ACRA financial statements (Singapore)").

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
