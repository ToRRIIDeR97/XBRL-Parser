# SEC Access Policy

## Identity

Every automated request must use `XBRL_SEC_USER_AGENT`. Use a real name or organization and a monitored email address.

Example:

```text
Example Research LLC data-operations@example.com
```

Do not ship the placeholder from `.env.example` into a running downloader.

## Rate limiting

The application defaults to 8 requests per second and a concurrency of 4, leaving margin below the configured ceiling. The limiter is per process and shared by all SEC clients inside it.

## One SEC-active process (enforced)

Only one process may send SEC traffic at a time. Every `SecHttpClient` the worker
and the CLI build for real SEC traffic (`worker run`, `catalog sync`, `taxonomy sync`)
carries a `SecEgressLock`: a PostgreSQL session-level advisory lock
(`pg_try_advisory_lock(hashtextextended('xbrl:sec-egress', 0))`) on a dedicated
connection.

- The lock is taken lazily, right before the first request that passes URL
  validation and the user-agent check, and released when the client closes. A
  worker that only runs non-SEC jobs (for example `--job-type reconcile_filing`)
  never takes it and can run alongside the SEC-active process.
- A second process fails fast on its first SEC request with `SecEgressBusyError`
  ("another process holds the SEC egress lock"); nothing is sent. A worker defers
  such a job by `XBRL_SEC_EGRESS_BUSY_RETRY_SECONDS` (default 60) without spending
  an attempt. A CLI command exits with the error.
- If the holder crashes, PostgreSQL ends its session and frees the lock.
- Online Arelle taxonomy fetches (`XBRL_ARELLE_OFFLINE=false`) run only after the
  worker holds the lock. With pinned packages and offline mode there are none.
- The lock lives in the control-plane database, so it coordinates processes that
  share it; separate deployments with separate databases still need their own
  egress policy. A shared, cross-process token bucket for several concurrent SEC
  workers is not implemented.

## Retry policy

Retry:

- connection resets and timeouts;
- HTTP 429;
- transient 5xx responses.

Do not repeatedly retry permanent 4xx responses. Respect `Retry-After` when present and add exponential backoff with jitter.

## Caching

- Download each immutable artifact/package once.
- Record checksum, ETag, Last-Modified, URL, status, and acquisition time.
- Prefer nightly bulk/API ZIPs and indexes for broad historical work rather than making one request per fact or company.

## Operational behavior

- Run large backfills outside market-sensitive deadlines when possible.
- Stop or reduce traffic when persistent 429/403 responses occur.
- Keep diagnostics sufficient to identify accidental request fan-out.
