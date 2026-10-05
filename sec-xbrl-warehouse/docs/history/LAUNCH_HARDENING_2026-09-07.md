# Launch hardening — 7 September 2026

This follow-up reviewed the application after the earlier audit fixes. Two general
subagents reviewed ingestion security and performance/recovery. The main agent
reviewed API/deployment behavior and integrated the fixes. Findings concern local
code and configuration, not a demonstrated compromise of SEC or this warehouse.

## Changes

| Area | Finding and change |
| --- | --- |
| API availability | Unauthenticated readiness calls bypassed concurrency limits. Only one database readiness probe now runs per process; overlapping probes return 503. Liveness avoids the thread pool and database. |
| Authentication | Health exemptions use the ASGI routing path. Production startup rejects empty or whitespace-only reader passwords. Existing bearer authentication and query budgets remain enforced. |
| Deployment | Running storage containers still published ports on all interfaces. They were recreated with loopback bindings and their existing named data volumes. PostgreSQL, ClickHouse, and MinIO now report healthy. |
| Container privileges | API/worker images run as UID/GID 10001, with capabilities dropped and privilege escalation disabled. Fresh application volumes are writable by this user. |
| Bucket initialization | Credentials were interpolated into shell code, and setup always exited successfully. Credentials now pass as quoted environment values, failures stop setup, and applications depend on successful initialization. |
| Build | The Linux ARM64 image failed because Arelle EFM's PythonMonkey dependency requires npm during installation. npm is installed for the build and removed afterward; EFM support is retained. See [PythonMonkey installation requirements](https://github.com/Distributive-Network/PythonMonkey#using-the-library). |
| Container networking | Custom host ports leaked into container settings, preventing API startup. API/worker connections now use ClickHouse's internal port 8123, and the API listens on internal port 8080 regardless of the host publication port. |
| Worker recovery | Checkpoint operations now validate and lock the claimed job in the same transaction. A task-local lease records worker and attempt, so a reclaimed job rejects stale checkpoint reads/writes even when the worker ID is reused. |
| Raw retention | Ingestion no longer deletes source objects. Setting `XBRL_RETAIN_FILING_SOURCE_FILES=false` is rejected. The existing local `.env` had this unsafe setting; it was changed to `true`. Raw data deletion remains a separate operator action. |
| Cached object downloads | Local and S3 restores enforce size limits before and during copying and preserve an existing destination on failure. S3 content and integrity metadata come from the same GET response. Filing packages, manifests, bulk restores, and extraction checkpoints pass their configured limits. |
| Manifest cache | Cache entries are bounded and checked against the requested filing, index-derived filenames, member counts/sizes, and source URLs. Member bytes are checksum-verified before parsing. This cache still trusts private object storage; it is not independent authentication of SEC source bytes against a malicious storage administrator. |
| Reconciliation | Queries select only comparison columns, enforce row/result-byte budgets, and have server memory/time limits. Bulk text's `value` column is aliased to the comparer’s expected `value_raw`, fixing previously missing text comparisons. |
| Taxonomy downloads | Online taxonomy requests enforce HTTPS/443 without URL credentials, including redirects. Response reads have per-response and aggregate byte limits and a request-count ceiling. Requests ask for identity encoding and reject compressed responses to avoid Arelle's unbounded gzip inflation. Offline/pinned taxonomy operation remains supported. |

Reconciliation defaults to 50,000 rows and 16 MiB per query, with a 512 MiB server
memory budget and 60-second timeout. Limits raise errors rather than returning a
silently truncated comparison. Comparison still materializes one bounded filing;
larger workloads require deliberate partitioning or streaming. Taxonomy reads use
`response_max_bytes` per response, `extraction_max_bytes` cumulatively, and
`filing_max_members` as the request ceiling for one extraction.

## Validation

- All **175 tests passed**, including 159 non-integration tests and 16 isolated
  PostgreSQL/ClickHouse/MinIO integration tests. The PostgreSQL fixture migrates
  temporary databases; ClickHouse DDL is applied in temporary databases.
- After the final internal-port configuration fix, all five deployment tests
  passed, including the new host/internal-port regression.
- `compileall`, Ruff lint/format checks, mypy, `pip check`, and `git diff --check`
  passed. The stub scan found zero implementation markers.
- Regression checks cover concurrent readiness, empty production credentials,
  shell quoting/failure handling, stale-worker checkpoint access, source retention,
  oversized reconciliation text, taxonomy limits, and bounded cache restores.
- The Linux ARM64 image built successfully after fixing the EFM dependency setup;
  UID 10001, writable application/cache directories, and PythonMonkey execution
  were smoke-tested. The storage services are healthy with loopback-only ports;
  private bucket initialization exited successfully.
- The final Linux parser extracted all three synthetic fixture facts offline
  under the default process memory/time limits.
- The local API is running at `http://127.0.0.1:8080`. Liveness, readiness,
  documentation, and a bounded fact query returned 200; an invalid broad/deep
  query returned 422. The worker wasn't started and no production jobs were run.

The dependency scan of the local Python environment queried PyPI advisories for
127 installed third-party packages and reported no known vulnerabilities. The
unpublished application itself is covered by code review and tests, not that scan.
The final Linux image's 106 third-party Python packages also reported no known
vulnerabilities. Hash comparison confirmed that all 64 application source files
in the running image match the reviewed workspace.

## Remaining launch conditions

- **Public deployment:** the local environment is still development mode. Supply
  a random API bearer key, a dedicated SELECT-only ClickHouse reader with enforced
  budgets, deployment secrets, TLS, and private service networks before exposing
  the API. Configure Prometheus credentials when API authentication is enabled.
- **Reproducibility:** image tags and Python dependency ranges remain mutable.
  Capture an immutable release image and dependency lock, and scan that image's
  OS packages before production. A Python advisory scan doesn't cover OS packages
  or the JavaScript dependencies installed by PythonMonkey.
- **Worker scaling:** SEC rate limiters are local to clients/processes. Keep one
  canary worker and coordinate discovery/other SEC clients; global egress limiting
  is required before adding concurrent workers.
- **Capacity:** repeated bulk ZIP integrity passes, rebuilding earlier Arrow
  batches on retry, and multiple bulk reconciliation queries remain throughput
  costs. They weren't removed at the expense of integrity or replay. Measure a
  representative corpus before optimizing these paths or estimating capacity.
- **Diagnostics:** retries can add repeated validation-issue rows. They retain
  attempt diagnostics but aren't deduplicated logical issues; aggregate reporting
  must account for repeats.
- **Acceptance:** no live SEC download/backfill, production-scale load benchmark,
  external penetration test, or backup restore drill was performed. Complete the
  real-filing matrix and restore/canary gates in `HANDOFF.md` before a public launch.

See `docs/DEPLOYMENT.md` for existing-volume ownership and monitoring upgrades.
These changes reduce identified risks; they don't certify the application free
of vulnerabilities or establish production capacity.
