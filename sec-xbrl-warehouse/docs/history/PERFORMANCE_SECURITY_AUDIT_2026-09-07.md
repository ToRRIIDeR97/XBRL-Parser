**Performance and security audit — SEC XBRL Warehouse**

Reviewed 7 September 2026, commit `911b1ad`. Application code and configuration were not changed. This review covered ingestion, HTTP and taxonomy access, object storage, worker execution, canonical mapping, query construction, deployment defaults, and the installed Python environment.

Subsequent remediation is recorded in the [implementation plan and completion record](AUDIT_IMPLEMENTATION_PLAN_2026-09-07.md). Findings below describe the original audited state.

The main release blockers are concurrent API query failures and missing query resource budgets. Several ingestion paths also need tighter resource controls before a large backfill. The checked-in localhost deployment reduces remote exposure; this report does not claim an internet-facing service or a demonstrated remote code execution exploit.

Priority P1 means fix before concurrent service use or wider deployment. P2 means address before the affected workflow is used at scale or with less-trusted inputs.

| ID | Priority | Area | Finding | Evidence |
| --- | --- | --- | --- | --- |
| 1 | P1 | Availability | Shared ClickHouse session rejects overlapping queries | Reproduced against configured database |
| 2 | P1 | Security/performance | API pagination does not bound query cost | Input probe and live database settings |
| 3 | P2 | Availability | Synchronous storage work blocks worker heartbeats | Local timing probe |
| 4 | P2 | Security | Imported bulk filenames can escape staging | Destination probe; no outside file written |
| 5 | P2 | Security/performance | ZIP validation has no decompression budget | Small compressed archive accepted |
| 6 | P2 | Performance | Incremental ingestion materializes full filings repeatedly | Source inspection |
| 7 | P2 | Performance | Canonical mapping performs database work per fact | Repository call-count probe |
| 8 | P2 | Security | SEC HTTP policy accepts plaintext HTTP | URL-policy probe |
| 9 | P2 | Development security | pytest dependency has a known vulnerability | pip-audit and upstream release notes |

**1. Shared ClickHouse session rejects overlapping queries**

[Client construction](../../src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py#L50) uses the synchronous driver's default automatic session. [API startup](../../src/sec_xbrl_warehouse/api/app.py#L20) shares that client across request handlers. Those synchronous handlers can run concurrently.

Two threads each executing `SELECT sleep(0.25)` through one `ClickHouseWarehouse` instance yielded one success and one `ProgrammingError`: "Attempt to execute concurrent queries within the same session." The application does not handle this error in the fact routes, so overlapping requests can return HTTP 500. Readiness checks use the same client and may also report a false failure during an active query.

Set `autogenerate_session_id=False` for the shared stateless client and add a focused overlapping-request check. The [official driver documentation](https://clickhouse.com/docs/integrations/language-clients/python/driver-api#multi-threaded-applications) recommends disabling sessions for shared concurrent clients.

**2. API pagination does not bound query cost**

[FactQuery](../../src/sec_xbrl_warehouse/query/service.py#L23) caps returned rows but permits any nonnegative offset and accepts a single date bound as its scan constraint. `filed_to=2100-01-01&limit=1&offset=1000000000` passed validation. This can match the entire historical dataset and require substantial scanning or sorting despite returning one row.

A read-only inspection of the configured ClickHouse connection returned `0` for `max_execution_time`, `max_memory_usage`, `max_rows_to_read`, and `max_bytes_to_read`. These settings therefore supply no per-query budget. Server-wide safeguards may still exist; they were not exhaustively audited. `readonly` was also `0`.

Use a dedicated SELECT-only API database identity with enforced time, scan, memory, and result-byte budgets. Require useful company/accession constraints or bounded date windows, and replace deep offsets with cursor pagination. Add authentication and request/concurrency limits before exposing the API beyond its documented local scope. Keep these limits separate from worker ingestion settings. [ClickHouse query-complexity controls](https://clickhouse.com/docs/concepts/features/configuration/settings/query-complexity) provide the server-side enforcement.

**3. Synchronous storage work blocks worker heartbeats**

[Package download](../../src/sec_xbrl_warehouse/ingestion/filing_downloader.py#L71) calls synchronous object-store methods directly from an async function. Its cache lookup also performs synchronous downloads and a HEAD/existence check per member. [Bulk download](../../src/sec_xbrl_warehouse/ingestion/bulk_downloader.py#L49) runs full ZIP CRC validation and upload on that event loop.

A fake store taking 200 ms to upload caused a 222 ms gap in a heartbeat scheduled every 10 ms. This verifies event-loop blocking, not an actual expired production lease. A sufficiently slow upload, storage retry sequence, or large validation pass can delay heartbeats past their lease deadline and cause duplicate execution by another worker.

Move these operations to `asyncio.to_thread`, following the existing pattern in other ingestion stages. [Readiness](../../src/sec_xbrl_warehouse/api/routers/health.py#L23) has the same issue with its synchronous ClickHouse ping. Also move full-bundle JSON serialization and CPU-heavy reconciliation off the event loop.

**4. Imported bulk filenames can escape staging**

[BulkArtifactDownloader](../../src/sec_xbrl_warehouse/ingestion/bulk_downloader.py#L36) joins `temp_dir` with an unrestricted `DatasetArtifact.filename`. Backfill plans loaded from JSON can supply that field. A probe using `../outside.zip` reached the downloader with a destination outside the configured temporary directory; it stopped before writing.

A tampered plan can direct downloaded SEC bytes to another writable path. An absolute filename can also override the staging root. Later checksum or ZIP validation cannot undo an earlier destination replacement. Normal catalog-generated filenames use basenames, so this finding specifically concerns imported or modified plans/job payloads; it is not an API upload exploit.

Validate a single safe filename at the domain boundary and verify destination containment before any download. Use separate staging paths keyed by artifact identity to avoid collisions between concurrent jobs with the same filename.

**5. ZIP validation has no decompression budget**

[validate_zip_integrity](../../src/sec_xbrl_warehouse/ingestion/bulk_downloader.py#L65) checks paths and duplicate names, then calls `testzip()`, which reads all members. There are no member-count, expanded-byte, or compression-ratio checks before that work. HTTP downloads likewise have no configured maximum byte count.

An 8,278-byte ZIP expanding to 8,388,608 bytes passed validation. This was a deliberately small probe, not a resource-exhaustion attack. The missing controls permit much larger CPU/disk workloads if an oversized or hostile source reaches ingestion. SEC-only source restrictions reduce who can supply those bytes.

Apply configurable compressed and expanded size budgets, member-count limits, and suitable compression-ratio checks before full CRC processing, with actual-byte enforcement during reading. Retain rejection diagnostics. Validation also runs at download and again at load, adding two full decompression passes before parsing; measure this cost before changing the integrity contract. Python documents these [decompression risks](https://docs.python.org/3.12/library/zipfile.html#decompression-pitfalls).

**6. Incremental ingestion materializes full filings repeatedly**

The Arelle adapter builds all domain entities before returning. [Checkpoint serialization](../../src/sec_xbrl_warehouse/ingestion/incremental.py#L135) then builds the entire JSON string and UTF-8 bytes. [ClickHouse bundle insertion](../../src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py#L261) constructs all five Arrow tables before inserting the first. The later `arrow_batch_rows` slicing cannot limit those earlier allocations.

Peak memory therefore grows with the full filing and its text, dimensions, taxonomy, and relationship payloads. Large filings can terminate the worker before normal quarantine handling runs. No representative peak-RSS benchmark was performed, so this report does not assign a measured memory ceiling.

First build and discard one entity table at a time, then convert entities into Arrow batches without full intermediate row lists. Use a killable parser process with a measured memory/time budget where input variability requires containment; retain Arelle for semantics. Bulk TSV batches also have a row budget rather than a byte budget, so long TXT fields need separate sizing.

**7. Canonical mapping performs database work per fact**

[map_batch](../../src/sec_xbrl_warehouse/mapping/canonical_mapper.py#L211) awaits a mapping lookup for each fact and a review operation for every unmapped fact. The PostgreSQL repository opens a session for each lookup and a separate transaction for each review.

Passing the same concept/fact 1,000 times produced 1,000 lookup calls and 1,000 review calls. This measures application calls using a fake repository, not 2,000 timed production SQL statements. In the real repository an already-existing review also requires a fallback SELECT.

Cache approved lookup results per concept/taxonomy/company/date within one mapping batch, and insert reviews in batches. Preserve compatibility decisions that depend on each fact's period and unit; caching a complete mapping decision by concept alone would be incorrect.

**8. SEC HTTP policy accepts plaintext HTTP**

[URL validation](../../src/sec_xbrl_warehouse/infrastructure/http/sec_client.py#L59) checks the hostname only. Both `http://www.sec.gov/data.zip` and `https://www.sec.gov:1234/data.zip` passed its policy. Initial requests and redirects use this validator.

A supplied plaintext URL or downgrade redirect can expose downloaded source data to an on-path attacker. A checksum computed after downloading is not an independent authenticity check, and source checksums are optional. The normal checked-in sources use HTTPS; no network interception was attempted.

Require HTTPS, approved ports, and no embedded URL credentials on initial URLs and every redirect. Preserve the existing exact host allowlist. The Arelle taxonomy network policy already requires HTTPS for actual requests.

**9. Pytest dependency has a known vulnerability**

The installed environment contains pytest 8.4.2. `pip-audit` identified `CVE-2025-71176` / `GHSA-6w46-j5rx-g56g`, concerning temporary-directory handling on Unix and local denial of service or possible privilege escalation. [pytest 9.0.3 release notes](https://github.com/pytest-dev/pytest/releases/tag/9.0.3) identify the fix.

The [development dependency constraint](../../pyproject.toml#L45) is `pytest>=8.4,<9`, excluding the reported patched version. Update that constraint and test against a patched release. This affects development/CI installations; the application Dockerfile does not request the dev extra.

The scan enumerated 128 packages, skipped the local unpublished project, and reported no other known package vulnerabilities. This is a database-backed dependency check, not proof that the remaining dependencies are vulnerability-free.

**Other deployment observations**

The checked-in configuration publishes ports only on localhost, and `.env` is ignored and untracked. SQL values are parameterized; YAML uses safe loading. Local object paths enforce bucket containment, S3 writes use conditional creation, and taxonomy access has host and local-root controls. These safeguards should be retained.

Authentication, TLS, separate service credentials, and deployment-specific secrets are documented acceptance gates. The API uses the general warehouse credentials, the application image runs as root by default, and multiple images use mutable tags. Tighten these before shared deployment and introduce a reproducible dependency lock plus recurring vulnerability scans.

Worker scaling needs a coordinated SEC request budget: each `SecHttpClient` creates its own limiter, and Arelle uses another limiter. Increasing worker count also increases aggregate traffic. Checkpoint writes currently lack the worker/attempt ownership conditions used by job completion; review that lease-recovery path before relying on overlapping workers.

The local ClickHouse container reported unhealthy, with recent health checks failing with connection refused, although the configured host connection answered the audit queries. Its health-check failure needs investigation; no container configuration was changed and no root cause is claimed here.

**Validation and limits**

- `python -m pytest -m 'not integration' -q`: **117 passed, 14 deselected**, two deprecation warnings, 3.18 seconds.
- `python -m pip check`: no broken requirements in the project environment.
- `pip-audit 2.10.1` against the existing virtualenv: **one known vulnerability**, described above.
- Live ClickHouse work was limited to system-setting reads and two 250 ms SELECT probes. No business data was modified.
- Local probes covered archive acceptance, path containment, URL policy, query input bounds, event-loop blocking, and mapping call counts. The synthetic Arelle fixture ran offline.
- No live SEC downloads, historical backfill, load stress test, container-image CVE scan, or full database integration suite was run. Query-plan and representative memory/throughput measurements remain necessary before capacity estimates.
- Audit and prose-cleanup utilities were installed only under `/tmp`; application dependencies were not upgraded. The report received a deterministic unslop pass and manual review.

Fix the shared client and query budgets first, then address blocking ingestion operations, input boundaries, and the development dependency advisory. Measure a representative filing corpus before choosing batch sizes or adding workers.
