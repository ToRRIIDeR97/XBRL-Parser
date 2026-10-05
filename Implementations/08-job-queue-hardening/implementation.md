# 08 — Job queue hardening: retries, dead-job recovery, backoff and queue metrics

Status: plan only. Code follows after this plan is reviewed.

## Problem

The PostgreSQL queue works, but failures are handled poorly in four ways:

1. **Storage outages kill jobs.** `is_retryable_exception`
   ([worker.py:325](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/worker.py)) only recognises built-in network errors, httpx and
   SQLAlchemy text. A ClickHouse connection error, a MinIO `SlowDown`/503, or a
   `WarehouseWriteError` caused by one is permanent, so the job goes straight to
   `dead`.
2. **Dead jobs can't be revived.** Re-enqueueing only updates `updated_at`
   ([postgres.py:267](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/storage/postgres.py)), and there's no CLI to list or retry jobs.
3. **Silent dead-lettering at claim time.** A job whose lease expired after its last
   attempt (for example, a parser that crashed the process) is marked dead in
   `claim_job` ([postgres.py:330](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/storage/postgres.py)) with no `last_error` and no metric. The
   call then returns `None`, so the worker sleeps a full poll interval although other
   work is ready.
4. **Backoff uses the host clock, with no jitter.** `fail_job` computes `available_at`
   with `datetime.now(UTC)`, while claims compare against database time. Measured:
   with the host clock one day ahead, a retry was scheduled one day later instead of
   seconds.

There's also no metric for queued, retry or dead jobs, or for the oldest ready
job's age. `ops/alerts.yml` has only disk and worker-up rules, while
`docs/OPERATIONS.md` calls for dead-job and stalled-queue alerts.

### Before / after

| | Before | After |
|---|---|---|
| MinIO `SlowDown` during a write | job dead on first attempt | retried with backoff |
| Recover a dead job | not possible | `jobs retry <id>` |
| Lease expired on the last attempt | dead, no reason, worker idles | dead with reason, counted, next job claimed |
| Host clock 1 day ahead | retry in about 1 day | retry in seconds (database time) |
| Backlog visibility | none | `xbrl_queue_jobs{job_type,state}`, `xbrl_queue_oldest_ready_age_seconds`, plus alerts |

## Scope

Likely files:

- `worker.py`:
  - `is_retryable_exception` recognises `clickhouse_connect` `OperationalError`, and
    `DatabaseError` with transient server codes (`TOO_MANY_PARTS` 252,
    `MEMORY_LIMIT_EXCEEDED` 241, `TIMEOUT_EXCEEDED` 159, plus others listed in the
    code);
  - botocore `EndpointConnectionError`, `ConnectionClosedError`, `ReadTimeoutError`,
    and `ClientError` codes `SlowDown`, `ServiceUnavailable`, `InternalError`,
    `RequestTimeout`, `503`.
- `infrastructure/storage/postgres.py`:
  - `list_jobs(state=None, job_type=None, limit=100)`;
  - `retry_job(job_id)`: dead → retry, attempts reset, `available_at = now()`,
    previous error kept in `last_error`;
  - `queue_snapshot()`;
  - `claim_job` dead-letters with a reason and keeps looking;
  - `fail_job` uses `now()` plus jittered exponential backoff, capped at one hour.
- `domain/ports.py`: the same methods on `ControlRepository`.
- `observability/metrics.py`: queue gauges filled by a scrape-time collector in the
  worker metrics server, and a dead-letter reason label.
- `cli.py`: `jobs list [--state] [--type]` and `jobs retry <id>`.
- `ops/alerts.yml`: dead jobs, and the oldest ready job above a threshold.
- `docs/OPERATIONS.md`, `docs/INGESTION_RUNBOOK.md`.

Exclusions:

- No distributed queue (hard constraint 8).
- The SEC egress lock is plan 09.

Invariants:

- `FOR UPDATE SKIP LOCKED` semantics: a job is never returned to two workers.
- Checkpoint ownership fencing (`locked_by` plus `attempts`) is unchanged.
- `--job-type` scoping: a scoped worker neither claims nor dead-letters other types.
- Permanent errors (integrity, schema drift, ClickHouse syntax or missing table,
  S3 `AccessDenied`/`NoSuchBucket`) stay permanent.

## Acceptance criteria

- **AC-1 Retry classification.** Transient ClickHouse and object-store errors are
  retryable, directly or as the `__cause__` of a warehouse error. The permanent
  errors listed above aren't.
- **AC-2 Dead-job recovery.**
  - `list_jobs` filters by state and type.
  - `retry_job` moves a dead job to `retry`, resets attempts, makes it claimable
    immediately, and keeps the previous error text.
  - Calling `retry_job` on a job that isn't dead raises `StateTransitionError`.
- **AC-3 Claim-time dead-letter.** When the selected job has exhausted its attempts,
  `claim_job`:
  - marks it `dead` with a `last_error` naming the exhausted attempts;
  - increments `xbrl_job_transitions_total{state="dead"}`;
  - claims and returns the next eligible job in the same call.
- **AC-4 Backoff.** `fail_job` schedules retries from database time, with jitter.
  Delays are positive, at most one hour, and not all identical for equal attempt
  counts. A skewed host clock has no effect.
- **AC-5 Queue health.**
  - `queue_snapshot()` returns counts by `(job_type, state)` and
    `oldest_ready_age_seconds`.
  - The worker metrics endpoint exports them as gauges.
  - `ops/alerts.yml` has a rule on dead jobs and one on
    `xbrl_queue_oldest_ready_age_seconds`.

## Implementation sequence

1. Retry classification (AC-1). Pure function, with unit tests in `tests/unit/test_worker.py`.
2. Repository methods and the `claim_job` loop (AC-2, AC-3), then backoff (AC-4).
3. Collector, metrics and alert rules (AC-5). CLI commands.
4. Docs. Validate suite including integration (`test_postgres_control.py` keeps
   proving no double claims).

## Verification

From `sec-xbrl-warehouse/`, with local storage services running:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/08-job-queue-hardening/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/08-job-queue-hardening/verification
```

The integration tests use a temporary database (`tests/integration/conftest.py`).
Raw SQL in the tests only arranges state, such as backdating rows; behavior is
always exercised through the repository.

### Baseline (main @ 8f16ca0, 5 October 2026)

- Every AC-1 transient case returns `False`.
- AC-2 and AC-5 methods are missing.
- AC-3 claims `None`.
- AC-4: with the host clock skewed, the delay is 1 day 2 s; there's no jitter
  (one distinct value).
- AC-5 alert rules lack dead/backlog expressions.
- The 6 permanent-error cases pass on baseline and pin behavior.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- AC-1: `is_retryable_exception` checks integrity/drift first, then transient
  ClickHouse (`OperationalError`; `DatabaseError` codes in `TRANSIENT_CLICKHOUSE_CODES`)
  and botocore errors (connection/timeout classes; `ClientError` codes in
  `TRANSIENT_S3_CODES` or HTTP 500/503), then the `__cause__` chain.
- AC-2: `list_jobs`, `retry_job` (dead -> retry, attempts 0, `available_at=now()`,
  previous error prefixed in `last_error`; other states raise `StateTransitionError`).
- AC-3: `claim_job` loops (bounded) inside one `SKIP LOCKED` transaction: an exhausted
  job is dead-lettered with a reason, counted in `xbrl_job_transitions_total{state=dead}`
  and `xbrl_job_dead_letters_total{reason}`, and the next eligible job is claimed.
  Type scoping is applied in the same query, so scoped workers never touch other types.
- AC-4: `fail_job` sets `available_at = now() + retry_delay_seconds(attempts)`
  (2^n seconds, 50-100% jitter, capped at 3600 s); `retry_at` still applies via `greatest`.
- AC-5: `queue_snapshot()`; `QueueCollector` registered by `worker run` exports
  `xbrl_queue_jobs`, `xbrl_queue_oldest_ready_age_seconds`, `xbrl_queue_snapshot_up`
  at scrape time; four new rules in `ops/alerts.yml`. CLI `jobs list|retry`.
- Gates: compileall, ruff, format, mypy, unit (345 passed, 1 skipped), scan_stubs (0),
  integration (30 passed, incl. concurrent no-double-claim with dead letters and
  scoped-claim tests), plan tests 01–08 (12/10/8/10/8/12/10/22 passed).
