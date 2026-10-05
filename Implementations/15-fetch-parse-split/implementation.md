# 15 — Split SEC-bound fetching from CPU-bound parsing, and run parse jobs in parallel

Status: plan only. Code follows after this plan is reviewed. Builds on plan 01
(fewer, concurrent downloads), plan 04 (offline taxonomies), plan 09 (egress lock)
and plan 13 (one archive per filing).

## Problem

One `ingest_filing` job downloads the package from the SEC *and* runs Arelle,
mapping and loading. The 2 October AAPL load processed 68 jobs in 43 minutes (about
38 seconds per filing), one at a time. Parsing can't be scaled out:

- Every worker builds a `SecHttpClient` at startup ([worker.py:59](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/worker.py)), and the
  "one SEC-active process" rule (plan 09) therefore applies to every worker, even ones
  that would only parse.
- A worker runs one job at a time. Arelle extraction already runs in a child process,
  so a worker could supervise several extractions on a multi-core host.
- Discovery enqueues `ingest_filing`, so SEC-bound and CPU-bound work share one job.

For history (2009 onward), the SEC's bulk Financial Statement and Notes data sets
remain the fast path: one ZIP per period covering every company, already supported by
`catalog sync` / `backfill`. Arelle stays the path for new filings, segments and
sampled comparison. This plan is about the per-filing path.

### Before / after

| | Before | After |
|---|---|---|
| Job types for one filing | `ingest_filing` (download plus parse) | `fetch_filing` (SEC, then enqueue) → `parse_filing` (no SEC) |
| Parse workers | 1 job at a time, holds an SEC client | `worker run --job-type parse_filing` with `XBRL_WORKER_CONCURRENCY=N`, no SEC client or lock |
| Throughput (rough) | about 38 s per filing, serial | fetch about 1–2 s per filing (plan 01) at 8 req/s; parse scales with cores |

## Scope

Likely files:

- `domain/enums.py`: `JobType.FETCH_FILING`, `JobType.PARSE_FILING`.
- `ingestion/incremental.py`:
  - `fetch(filing, *, job_id=None) -> FilingPackageManifest`: download, archive, and
    `update_filing_package`;
  - `parse(accession, *, extraction_revision, job_id=None) -> dict`: load the stored
    manifest via `control.get_filing_manifest(accession)`, then materialize, extract,
    validate, write and publish;
  - `ingest` = `fetch` + `parse`, kept for queued legacy jobs.
- `domain/ports.py` and `postgres.py`: `get_filing_manifest(accession)`. Store a
  pointer to the manifest object (`manifest_object_key`) on `filings`, with a
  migration.
- `worker.py`:
  - handlers for `FETCH_FILING` (calls `fetch`, then enqueues `PARSE_FILING` with
    payload `{accession, extraction_revision}` and key
    `parse:{accession}:{revision}`) and `PARSE_FILING`;
  - discovery enqueues `FETCH_FILING` with key `fetch:{accession}:{revision}`;
  - `requires_sec(job_types)`, and `runtime_services` builds the SEC client (and
    egress lock) only when it returns true;
  - `Worker` honors `settings.worker_concurrency` (default 1), claiming only while a
    slot is free, each job with its own heartbeat and lease.
- `config.py`: `worker_concurrency: int = Field(default=1, ge=1, le=32)`.
- `cli.py`: `filing enqueue` produces `fetch_filing`.
- `docker-compose.yml` and `docs/DEPLOYMENT.md`: an optional `worker-parse` service
  without SEC settings.
- `docs/OPERATIONS.md`, `docs/INGESTION_RUNBOOK.md`, `HANDOFF.md`, `AGENTS.md`
  (canonical commands). The existing unit test asserting the discovery key
  `filing:<acc>:1` changes to `fetch:<acc>:1`.

Exclusions:

- No new queue technology (hard constraint 8).
- Writing Parquet directly instead of the extraction checkpoint isn't needed. Plan 13
  makes the checkpoint small.

Invariants:

- `FOR UPDATE SKIP LOCKED`, lease fencing and checkpoint ownership hold for every
  concurrently running job.
- A parse job never performs SEC network I/O. A missing stored package is a
  permanent, clearly named error (`ArtifactIntegrityError`), not a fallback download.
- Queued legacy `ingest_filing` jobs still complete.
- Memory: `worker_concurrency × arelle_memory_limit_bytes` must fit the container
  limit. `doctor` checks this.

## Acceptance criteria

- **AC-1 Fetch → parse.** The `FETCH_FILING` handler stores the package and enqueues
  exactly one `PARSE_FILING` job with payload `{accession, extraction_revision}` and
  key `parse:{accession}:{revision}`.
- **AC-2 Parse without SEC.** `ingestor.parse(accession, extraction_revision=…)` uses
  the stored manifest and completes without calling `download_package`. With no
  stored package it raises a non-retryable `ArtifactIntegrityError`.
- **AC-3 Concurrency.** With `worker_concurrency=2`, the worker runs two jobs at once
  (peak 2, never more) and completes all queued jobs. The default is 1.
- **AC-4 SEC need.** `requires_sec(job_types)` is true for `None` (all types) and for
  any set containing `fetch_filing`, `ingest_filing`, `discover_filings` or
  `download_bulk`. It's false otherwise.
- **AC-5 Discovery.** Discovery enqueues `fetch_filing` jobs keyed
  `fetch:{accession}:{revision}`.
- **AC-6 Legacy.** A queued `ingest_filing` job still completes (fetch and parse in one
  job).

## Implementation sequence

1. Enums, `fetch`/`parse` split with `ingest` as their composition, manifest lookup
   and migration (AC-2, AC-6).
2. Handlers, discovery change, `requires_sec` and lazy SEC services (AC-1, AC-4, AC-5).
3. Worker concurrency (AC-3). Add a heartbeat/lease test per concurrent job in
   `tests/unit/test_worker.py`.
4. Compose, docs, Validate suite including integration (`test_postgres_control.py`
   double-claim test with concurrency > 1).
5. Operator benchmark: re-ingest the AAPL filings with one fetch worker and one parse
   worker at concurrency 4. Record the per-stage `xbrl_stage_latency_seconds` and the
   total wall time against the 43-minute baseline.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/15-fetch-parse-split/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/15-fetch-parse-split/verification
```

### Baseline (main @ 8f16ca0, 5 October 2026)

13 failed, 1 passed:

- The new job types, `ingestor.parse`, `requires_sec` and `worker_concurrency` don't
  exist.
- `test_ac6_legacy_ingest_job_still_completes` passes on baseline and pins
  compatibility.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- **Job types.** `JobType.FETCH_FILING`, `JobType.PARSE_FILING`; `ingest_filing` stays
  as the legacy one-job path.
- **Ingestor.** `fetch(filing, *, job_id)` downloads (or reuses the job's package
  checkpoint), archives a pre-plan-13 cached manifest if needed, and records the
  package and manifest pointers (`update_filing_package`). `parse(accession, *,
  extraction_revision, job_id, manifest=None)` loads the manifest from the argument,
  the job's package checkpoint, or `control.get_filing_manifest`; none →
  non-retryable `ArtifactIntegrityError` ("No stored package ... run fetch_filing
  first"). It never calls `download_package`. `ingest` = completed-checkpoint check,
  `fetch`, then `parse`. Failure diagnostics are shared (`_record_failure`).
- **Manifest pointer.** Migration `0006_filing_manifest_pointer` adds
  `filings.manifest_object_key` and `manifest_checksum_sha256`;
  `PostgresControlRepository.get_filing_manifest` reads and checksum-verifies the
  stored manifest through the object store `runtime_services` attaches.
  `extraction_checkpoint` also finds completed `parse_filing` jobs (payload
  `accession`), so `map_canonical`/remap work after the split.
- **Worker.** `FETCH_FILING` handler enqueues one `PARSE_FILING` (`parse:{acc}:{rev}`);
  `PARSE_FILING` handler parses and rebuilds the company's series (the parse result
  carries `cik`); discovery and `filing enqueue` queue `fetch:{acc}:{rev}`.
  `requires_sec(job_types)`; `runtime_services(settings, control, job_types)` builds
  the SEC client and egress lock only when needed. A parse-capable worker without an
  SEC client in online Arelle mode gets a `LazyEgress` (lock taken before the first
  extraction, released on exit); with offline Arelle it never touches the lock.
  `FilingPackageDownloader` accepts no SEC client and raises `SecAccessError` if
  asked to download. `Worker` runs up to `worker_concurrency` jobs (Field 1..32),
  claiming only when a slot is free; each job keeps its own lease, heartbeat, and
  checkpoint ownership; stop waits for running jobs. PostgreSQL pool size grows
  with concurrency.
- **Doctor** "Worker memory": `worker_concurrency x arelle_memory_limit_bytes` vs the
  cgroup memory limit (passes with a note when none is found).
- **Compose** optional `worker-parse` service (profile `parse`, offline Arelle,
  `--job-type parse_filing`). Docs: OPERATIONS, INGESTION_RUNBOOK, DEPLOYMENT,
  DATA_MODEL, HANDOFF, root AGENTS.md.
- **Tests.** Unit: discovery key (`fetch:<acc>:3`), fetch → parse handlers and series
  rebuild, per-job lease and heartbeat at concurrency 2, parse-only services build no
  SEC client (offline) or only the lazy lock (online), doctor memory check, `filing
  enqueue`. Integration: two workers at concurrency 3 run 12 queued parse jobs exactly
  once (peak > 1), and the manifest pointer plus parse-job extraction checkpoint.
- **Plan test conflict (reported, not edited).**
  `verification/test_ac5_discovery_enqueues_fetch_jobs` expects the key
  `fetch:<acc>:1`, but discovery uses `CURRENT_EXTRACTION_REVISION` (3, the plan 01
  revision policy) as the plan text `fetch:{accession}:{revision}` requires; the test
  was written against the baseline revision 1. It fails on that assertion only.
- Operator follow-up: Alembic 0006; benchmark one fetch worker plus one parse worker at
  concurrency 4 against the 43-minute baseline.
