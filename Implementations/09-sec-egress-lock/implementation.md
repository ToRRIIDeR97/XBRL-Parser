# 09 — Enforce one SEC-active process with a PostgreSQL advisory lock

Status: plan only. Code follows after this plan is reviewed.

## Problem

The SEC rate limit (≤10 requests/second; the default here is 8) is enforced by an
`AsyncRateLimiter` inside each process
([rate_limiter.py:9](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/http/rate_limiter.py), [sec_client.py:35](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/http/sec_client.py)). Two processes, such as two workers,
or a worker plus `catalog sync` or `filing discover`, each run at the full budget.
The only safeguard is a sentence in `AGENTS.md` and `HANDOFF.md` ("Run only one
SEC-active process at a time"). The September audit left this as an operator rule
(HARDENING_REMEDIATION row 10).

### Before / after

| | Before | After |
|---|---|---|
| Second worker starts while one is downloading | both send at 8 req/s (16 total) | second fails fast on its first SEC request: "another process holds the SEC egress lock" |
| Worker scoped to non-SEC jobs (for example `reconcile_filing`, or plan 15's parse jobs) | allowed | still allowed: the lock is taken lazily |
| Holder crashes | n/a | PostgreSQL drops the session lock; the next process can proceed |

## Scope

Likely files:

- New `infrastructure/http/egress_lock.py`:
  - `SecEgressLock(settings)` with `acquire()`, `release()` and `abandon()`.
    `abandon()` closes the session without unlocking; it's used by tests to simulate
    a crash.
  - Implemented with `pg_try_advisory_lock(hashtextextended('xbrl:sec-egress', 0))`
    on a dedicated connection that's held while the lock is held.
  - `SecEgressBusyError(SecAccessError)`.
- `infrastructure/http/sec_client.py`: optional `egress_lock` constructor argument.
  The lock is acquired once, just before the first request that passes URL
  validation, and released in `__aexit__`.
- `worker.py` and `cli.py`: every `SecHttpClient` built for real SEC traffic passes a
  `SecEgressLock`.
- `infrastructure/xbrl/arelle_adapter.py`: in online (not offline) mode, taxonomy
  requests to SEC hosts happen only in a process that holds the lock. With plan 04's
  offline mode there are none.
- `docs/SEC_ACCESS.md`, `docs/OPERATIONS.md`, `AGENTS.md`/`HANDOFF.md`: replace the
  "run only one" instruction with a description of the enforced rule.

Exclusions:

- A shared, cross-process token bucket for multiple concurrent SEC workers. Only
  worth adding if one process at 8 req/s proves too slow.

Invariants:

- The lock is a session-level advisory lock. It needs no table and is released by
  PostgreSQL when the holder's connection ends.
- URL validation and the user-agent check still happen before any lock or network
  activity.
- Tests that construct `SecHttpClient` without `egress_lock` behave exactly as today.

## Acceptance criteria

- **AC-1 Mutual exclusion.**
  - While one `SecEgressLock` holds the lock, `acquire()` on another instance raises
    `SecEgressBusyError` immediately, without blocking.
  - After `release()`, or after the holder's session ends without release
    (`abandon()`), another instance can acquire it.
  - Re-acquiring in the same holder is a no-op.
- **AC-2 Lazy, once.** A `SecHttpClient` with a lock acquires it once, right before
  its first SEC request, and releases it on exit. If acquiring fails, the request
  isn't sent and the error propagates.
- **AC-3 End to end.** Two clients with real locks on the same database: the second
  client's first request raises `SecEgressBusyError` and nothing reaches the
  transport.
- **AC-4 No traffic, no lock.** A client that sends no requests never acquires or
  releases the lock.
- **AC-5 Order of checks.** A URL rejected by host validation raises `SecAccessError`
  without acquiring the lock.

## Implementation sequence

1. `egress_lock.py` (AC-1).
2. `SecHttpClient` integration (AC-2, AC-4, AC-5).
3. Wire up the worker and CLI. For the worker, catch `SecEgressBusyError` on a job and
   fail it as retryable with a delay. A busy egress lock is transient.
4. Docs. Validate suite including integration (AC-3).

## Verification

From `sec-xbrl-warehouse/`, with local storage services running:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/09-sec-egress-lock/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/09-sec-egress-lock/verification
```

Manual check: start `worker run`, then run `catalog sync` in a second shell. It must
fail fast with the busy message.

### Baseline (main @ 8f16ca0, 5 October 2026)

All 8 tests fail: `SecHttpClient` has no `egress_lock` argument, and `egress_lock`
doesn't exist.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
