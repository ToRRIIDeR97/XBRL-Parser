# SEC Access Policy

## Identity

Every automated request must use `XBRL_SEC_USER_AGENT`. Use a real name or organization and a monitored email address.

Example:

```text
Example Research LLC data-operations@example.com
```

Do not ship the placeholder from `.env.example` into a running downloader.

## Rate limiting

The scaffold defaults to 8 requests per second and a concurrency of 4, leaving margin below the SEC's published maximum access rate. The process-wide limiter must be shared by all SEC clients inside a worker.

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
