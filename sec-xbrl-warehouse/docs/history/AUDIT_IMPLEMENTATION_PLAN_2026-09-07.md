**Implementation plan and completion record — 7 September 2026**

This change implements the nine findings in the [audit](PERFORMANCE_SECURITY_AUDIT_2026-09-07.md). It preserves Arelle semantics, immutable raw objects, extraction revisions, and source-to-canonical lineage.

| Step | Implementation | Status |
| --- | --- | --- |
| 1. Query safety | Disable automatic ClickHouse sessions. Give API connections read-only query settings, time/scan/memory/result budgets, and verify the effective settings at connection startup. | Complete |
| 2. API boundaries | Cap offsets at 10,000. Require a company, accession, or complete date interval no wider than 366 days. Limit concurrent requests; support bearer authentication and require a key plus separate reader credentials in production. | Complete |
| 3. Download boundaries | Validate filenames on model ingestion and again before writing. Isolate bulk staging by artifact identity. Require HTTPS/443 without URL credentials for initial requests and redirects. Bound buffered responses, downloads, and filing packages. | Complete |
| 4. Archive safety | Check compressed size, central-directory allocation, member count, individual/total expanded size, and compression ratios. Enforce actual expanded-byte limits while checking CRCs. Archive downloaded raw bytes before ZIP validation. | Complete |
| 5. Worker responsiveness | Move object-store operations, ZIP validation, resume-file reads, sidecar writes, checkpoint serialization, and reconciliation off the event loop. Move readiness pings to a thread. | Complete |
| 6. Filing memory | Run Arelle in a spawned, killable process with a timeout and a Linux address-space limit. Stream checkpoint JSON to disk. Build one entity group at a time; enforce row and byte budgets before Arrow writes. Add container memory limits. | Complete |
| 7. Mapping throughput | Cache approved lookups within an invocation, keeping unit/period checks per fact. Write reviews in groups of 500 and stream canonical output in batches through both worker paths. | Complete |
| 8. Dependency security | Allow patched pytest 9.x, install pytest 9.1.1 in the project environment, and rerun vulnerability scanning. | Complete |
| 9. Validation | Add rejection, timeout, concurrency, memory-batching, and replay tests; run static checks and isolated database integration tests. | Complete |

**Compatibility and operational behavior**

Existing company/accession queries and ordinary offsets continue to work. Concept-only requests, one-sided date scans without a company/accession, date-only intervals over 366 days, and offsets over 10,000 now return HTTP 422. Large exports should use bounded company/date partitions. Cursor pagination is not needed to enforce this bounded API contract and was not added.

Query failures and exhausted budgets return HTTP 503 without database internals. The default API concurrency limit is four requests per process. Health endpoints remain available without a token. When configured, the bearer key protects the remaining endpoints, including metrics and API documentation; configure monitoring credentials accordingly.

Production startup requires `XBRL_API_KEY` with at least 32 characters and separate `XBRL_API_CLICKHOUSE_USERNAME` / `XBRL_API_CLICKHOUSE_PASSWORD`. Supply secrets through the environment or your deployment secret manager. Keep TLS termination and network access restrictions at the deployment boundary.

Provision the reader with SELECT-only grants on the warehouse. Configure its ClickHouse settings profile to enforce the budgets below, especially if the user has `readonly=1`: that mode may prevent the driver from changing settings per request. Startup now rejects a connection whose effective settings are absent, unlimited, or looser than the requested budgets. This avoids silently dropping protections when a reader profile prevents overrides.

| ClickHouse setting | Default API budget |
| --- | --- |
| `readonly` | 1 |
| `max_execution_time` | 30 seconds |
| `timeout_before_checking_execution_speed` | 0 |
| `max_memory_usage` | 536,870,912 bytes |
| `max_rows_to_read` | 5,000,000 |
| `max_bytes_to_read` | 1,073,741,824 bytes |
| `max_result_rows` | 10,000 |
| `max_result_bytes` | 16,777,216 bytes |
| `max_threads` | 2 |
| `result_overflow_mode`, `read_overflow_mode`, `timeout_overflow_mode` | `throw` |

The API uses these limits on its own connection; backfill and worker connections retain their ingestion settings. Existing local development can use its current database identity with request-level read-only protection. Dedicated production accounts and external TLS certificates were not provisioned on the running services.

All tunable limits are documented in [.env.example](../../.env.example). Defaults allow 32 GiB bulk downloads, 64 MiB buffered HTTP responses, 128 GiB total ZIP expansion, a 200:1 compression ratio, and 10,000 ZIP members. Filing packages are capped at 1,000 files and 512 MiB. A row/record too large for the configured batch budget fails with diagnostics; no source fact is silently dropped. Adjust these values after measuring the intended corpus.

Arelle still needs a complete model for one filing. The model now lives in a separate process and is released before the parent loads the typed result. Serialization no longer creates whole-bundle Python dictionaries, JSON strings, and encoded copies. The parent still holds the typed bundle, bounded by the serialized extraction limit; this is not a constant-memory streaming XBRL parser.

The parser timeout defaults to 600 seconds. Linux additionally enforces a 2 GiB address-space limit. Native macOS/Windows runs do not enforce that OS memory limit; use the supplied Docker deployment for a hard memory ceiling. Compose caps the worker at 4 GiB and the API at 2 GiB. Its ClickHouse health check now uses `127.0.0.1`, verified to respond inside the existing container. These Compose changes apply when services are recreated; running services were not restarted.

**Validation evidence**

- All 145 non-integration tests, including regression checks for the new boundaries and replay behavior, passed.
- All 15 integration tests passed against isolated PostgreSQL/ClickHouse databases and a temporary MinIO bucket. Their fixtures applied the PostgreSQL migration and ClickHouse DDL to the test databases.
- The shared-client integration test executes two overlapping queries successfully, verifies that API connections reject CREATE TABLE, and checks scan-budget enforcement.
- The mapping integration test inserts 1,001 reviews in three INSERT statements. Replaying them uses three INSERTs plus three SELECTs and returns the same IDs.
- A repeated-concept test verifies one lookup for 1,001 facts while retaining incompatible-period review behavior and refreshing mappings between invocations.
- Parser timeout tests verify child termination and temporary-file cleanup. Serialization round trips, per-entity Arrow row/byte limits, and failed-write resume behavior are covered.
- `compileall`, Ruff lint/format checks, mypy, and `pip check` passed.
- The post-upgrade `pip-audit` scan reported no known vulnerabilities in the installed third-party packages. The unpublished local project remains outside that package-advisory scan.

No live SEC backfill, production-scale load test, container-image CVE scan, or Linux parser memory-limit test was run from this macOS environment. Those remain deployment acceptance work. The additional audit observations about shared SEC rate limits across workers, checkpoint lease fencing, image pinning, and root container users were not part of the nine ranked fixes; keep worker scaling within the current acceptance boundary until those are addressed.
