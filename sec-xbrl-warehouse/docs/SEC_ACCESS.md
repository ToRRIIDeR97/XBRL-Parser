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

## Other sources

Non-SEC regulators (enabled with `XBRL_ENABLED_SOURCES`) don't change any rule on this
page. `SecHttpClient` is the generic `SourceHttpClient` with the SEC policy: the same
hosts, user agent, rate, concurrency, retries, and byte limits as before. Another
source's client has its own host allowlist, so it can't send requests to SEC hosts,
and its own egress lock (`xbrl:<source>-egress`), so it neither takes nor waits for
`xbrl:sec-egress`. SEC traffic still comes only from the SEC adapter (discovery and
`fetch_filing`), bulk downloads, `catalog sync`, `taxonomy sync`, and online Arelle.
The `esef` (filings.xbrl.org), `edinet` (api.edinet-fsa.go.jp) and `dart`
(opendart.fss.or.kr) clients send the same `XBRL_SEC_USER_AGENT` but none of their
requests count against the SEC rate.

## One SEC-active process (enforced)

Only one process may send SEC traffic at a time. Every `SecHttpClient` the worker
and the CLI build for real SEC traffic (`worker run` for SEC job types, `catalog sync`,
`taxonomy sync`) carries a `SecEgressLock`: a PostgreSQL session-level advisory lock
(`pg_try_advisory_lock(hashtextextended('xbrl:sec-egress', 0))`) on a dedicated
connection.

- The lock is taken lazily, right before the first request that passes URL
  validation and the user-agent check, and released when the client closes. A
  worker that only runs non-SEC jobs (for example `--job-type reconcile_filing` or
  `--job-type parse_filing` with `XBRL_ARELLE_OFFLINE=true`) builds no SEC client,
  never takes it, and can run alongside the SEC-active process. A parse worker in
  online Arelle mode takes the lock before its first extraction (Arelle may fetch
  taxonomies from SEC hosts) and holds it until it stops.
- A second process fails fast on its first SEC request with `SecEgressBusyError`
  ("another process holds the SEC egress lock"); nothing is sent. A worker defers
  such a job by `XBRL_SEC_EGRESS_BUSY_RETRY_SECONDS` (default 60) without spending
  an attempt. A CLI command exits with the error.
- If the holder crashes, PostgreSQL ends its session and frees the lock. A holder
  checks its lock connection every `XBRL_SEC_EGRESS_CHECK_SECONDS` before SEC requests,
  so a silently dropped connection can't leave two SEC-active processes.
- Deferral is capped by `XBRL_SEC_EGRESS_MAX_DEFER_SECONDS` (default 6 hours) from
  the first deferral; the job then goes `dead` (migration `0005_job_deferral` adds
  `ingestion_jobs.deferred_since`).
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
