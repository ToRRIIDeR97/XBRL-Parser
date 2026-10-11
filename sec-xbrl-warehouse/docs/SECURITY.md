# Security

This document describes the security model as it stands in code and Compose.
Dated audit and hardening reports record how it got here; this file holds the current state.

## Assets

| Asset | Where it lives | Why it matters |
| --- | --- | --- |
| Raw source artifacts (bronze) | MinIO/S3 bucket | The replay source. Ingestion never deletes them, and `XBRL_RETAIN_FILING_SOURCE_FILES=false` is rejected. |
| Silver Parquet and checkpoints | Object store | The ClickHouse rebuild source. Checkpoints bind the source checksum, code hashes, schema, and row counts. |
| Control state | PostgreSQL | Jobs, leases, artifacts, validation issues, mapping versions, and review decisions. |
| Warehouse facts | ClickHouse | Raw and canonical facts served by the API. |
| Credentials | `.env` (local), secret manager (production) | Owner/admin/root credentials (admin service), the worker, writer, and reader identities, `XBRL_API_KEY`. Settings hold them as `SecretStr`, so reprs, dumps, and tracebacks don't print them. |
| SEC identity | `XBRL_SEC_USER_AGENT` | The SEC identifies and throttles traffic by this identity. |
| Regulator API keys | `XBRL_EDINET_API_KEY`, `XBRL_DART_API_KEY` in `.env` (local), secret manager (production) | Per-user keys for EDINET and OpenDART. The regulators' terms forbid sharing them; a leaked key can be used up (DART's daily cap) or revoked. |

## Trust boundaries

![Trust boundaries: untrusted SEC, filings.xbrl.org (ESEF), EDINET, OpenDART, taxonomy, and client traffic enters a private Compose network; the worker holds writer credentials and bounds input before writing MinIO, PostgreSQL, and ClickHouse; the API holds only reader identities and a bearer key](diagrams/trust-boundaries.svg)

- **SEC and taxonomy content is untrusted.** ZIP members, filing packages, TSV
  rows, and taxonomy responses are bounded before allocation. Unsafe names,
  traversal paths, and symlinks are rejected, and checksums are verified before parsing.
- **Each source (regulator) is its own outbound path.** A source's adapter reaches the
  network only through its `SourceHttpClient`, whose `SourcePolicy` lists the hosts
  it may contact (HTTPS on port 443, no credentials). Every redirect hop is checked
  against the same allowlist before it is requested, so one source's client can't
  reach another source's hosts; byte limits, retries, and `Retry-After` handling are
  the same for every source. The SEC policy is unchanged: `www.sec.gov`, `sec.gov`,
  `data.sec.gov` and `XBRL_SEC_USER_AGENT`. Only sources in `XBRL_ENABLED_SOURCES`
  get an adapter; a job for any other source fails permanently and never falls back
  to the SEC. Bytes from every source get the same bounds and checksums as SEC bytes.
  `source_policies` refuses two policies that share a host, and `worker run` checks
  it at startup.
- **filings.xbrl.org (ESEF) is a third-party aggregator.** XBRL International
  collects ESEF reports from national registries; it isn't the regulator, so a report
  can be missing, late, or (in principle) altered upstream of the site. The `esef`
  client may reach only `filings.xbrl.org` (HTTPS, at most 2 requests/s, sending
  `XBRL_SEC_USER_AGENT`, the operator's contact identity, as the SEC client does) under its own `xbrl:esef-egress` lock, and never the SEC hosts.
  Its JSON is untrusted input: records with an invalid filing key, LEI, date, or a
  package URL that isn't a plain path on that host are skipped. A package must match
  the SHA-256 the site publishes for it (nothing is stored on a mismatch or without a
  digest), which proves the bytes are the ones the site published, not that the issuer
  filed them. It then gets the same ZIP checks as SEC archives and is stored
  byte-identical. A report package's own `META-INF/catalog.xml` may remap only its
  extension taxonomy to files inside the package: a target outside the package, or a
  prefix that overlaps a pinned taxonomy, fails the extraction. Reuse terms are
  unconfirmed (OPERATIONS.md, "ESEF filings").
- **EDINET and OpenDART are regulators behind a per-user key.** The `edinet` client
  may reach only `api.edinet-fsa.go.jp` and the `dart` client only
  `opendart.fss.or.kr` (HTTPS, at most 1 request/s, one at a time, sending
  `XBRL_SEC_USER_AGENT`), each under its own `xbrl:<source>-egress` lock. Their JSON
  lists and zips are untrusted input: records with a malformed document or receipt
  number, entity code, or date are skipped and logged; a JSON or XML error body
  instead of a zip stores nothing; zips get the same checks as SEC archives, need
  exactly one instance, and are quarantined otherwise. Neither API publishes a
  digest, so the stored SHA-256 proves only what was received over TLS.
- **API keys stay in the request.** `XBRL_EDINET_API_KEY` and `XBRL_DART_API_KEY` are
  `SecretStr` settings, sent only as the `Subscription-Key` / `crtfc_key` query
  parameter of that regulator's requests. The policy names those parameters
  (`SourcePolicy.secret_params`), and `SourceHttpClient` masks their values
  (`[REDACTED]`) in every URL it logs, raises, or writes to a download sidecar; a
  logging filter does the same for the `httpx`/`httpcore` request logs, and library
  exceptions are masked before they are chained. The adapters build every stored URL
  (filing references, manifest members, object metadata, job payloads) without the
  key and re-raise any failure that still mentions it masked and unchained. A missing
  key raises `ConfigurationError` before any request, and a fetch-capable worker
  refuses to start without it. The Arelle child gets settings with both keys blanked,
  and the `worker-parse` container blanks them in its environment; the `api` service
  never receives them. Rotate a key on the regulator's site if it leaks.
- **Taxonomy packages are pinned.** `taxonomy sync` is a separate outbound path: it
  downloads only registry URLs that use HTTPS on port 443, carry no credentials, and
  name a host in `XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS`. It uses the rate-limited
  client and byte limits, and installs a file only after its SHA-256 matches the
  registry. Extraction re-verifies every package. `archive` and `file` entries are
  the publishers' unmodified files; only verified files are mapped into Arelle. `xbrl.sec.gov` is on the default
  allowlist because it serves the SEC taxonomies (dei and others) and their packages;
  `www.esma.europa.eu` (ESEF taxonomy), `xbrl.ifrs.org` and `www.ifrs.org` (IFRS
  Accounting Taxonomy) are on it for the `esef` source's pinned packages, and
  `disclosure.edinet-fsa.go.jp` (EDINET taxonomy namespaces) and
  `disclosure2dl.edinet-fsa.go.jp` (the FSA's taxonomy downloads) for the `edinet`
  source's. DART's taxonomy host isn't on it yet: add it once the canary confirms it.
  None of these is a source API host (`check_disjoint_hosts` keeps the source
  policies apart; the source hosts aren't taxonomy hosts).
  The W3C `xml.xsd` schemas are shipped with the application, so `www.w3.org` is never
  contacted.
- **The object store is trusted.** Manifest-cache and checkpoint verification detect
  corruption and mismatched reuse. They don't defend against a malicious storage administrator.
  The bucket is versioned (noncurrent versions expire after 30 days), and the workers'
  object-store user can't delete, so a compromised worker can't remove or overwrite
  bronze objects.
- **The Arelle child parses untrusted XML and has no credentials.** It gets settings
  with the database, object-store, and API secrets blanked and removes credential
  variables (`*PASSWORD*`, `*SECRET*`, `*DSN*`, `*API_KEY*`, `*ACCESS_KEY*`) from its
  own environment. Its initial process environment is inherited from the worker, so
  the worker containers carry only least-privilege identities (below).
- **The parse worker has no internet route.** `worker-parse` is only on the internal
  `backend` network, so offline Arelle can't reach a remote host even if its
  allowlist were bypassed.
- **Every service has its own least-privilege identity** (table in OPERATIONS.md,
  "Identities and the admin service"):
  - workers use `xbrl_worker` (PostgreSQL DML only, no DDL or superuser), `xbrl_writer`
    (ClickHouse SELECT and INSERT), and an object-store user with get, put, and list
    but no delete; Compose blanks the owner, admin, root, and API secrets that `.env`
    holds in their environment;
  - the API gets an explicit environment allowlist with no `.env`, no writer or MinIO
    credentials, and no application-data volume;
  - the owner, ClickHouse admin, and MinIO root credentials are used only by the
    on-demand `admin` service (migrations, DDL, upgrades, taxonomy sync, storage
    maintenance) and `minio-init`.

## Authentication and authorization

- **API.** When `XBRL_API_KEY` is set (at least 32 characters), every route except
  `/health/live` and `/health/ready` requires `Authorization: Bearer <key>`, compared
  in constant time. That includes `/docs`, `/metrics`, and queries. Without a key the API
  is unauthenticated and is safe only on loopback, so `api serve` refuses a
  non-loopback bind without a key unless `XBRL_API_ALLOW_UNAUTHENTICATED_REMOTE=true`
  (Compose sets it: the container binds `0.0.0.0` behind a `127.0.0.1`-only port).
- **Production startup** (`XBRL_ENV=production`) fails unless an API key and a separate,
  non-empty ClickHouse reader identity are configured, and it rejects any credential
  that contains the development marker `dev_password`, plaintext ClickHouse
  (`XBRL_CLICKHOUSE_SECURE=false`), and an object-store endpoint without TLS. `doctor`
  runs the same check. The API adapter never falls back to the ClickHouse writer in
  production. See `api/app.py` and `Settings.assert_production_ready`.
- **ClickHouse reader.** Production expects a SELECT-only user whose settings profile
  enforces the API query budgets below. The API requests these settings on its own
  connection and refuses to start if the effective limits are missing, unlimited, or
  looser than configured (a `readonly=1` profile may block per-request overrides, so set
  them in the profile). Worker connections keep their ingestion settings.

  | ClickHouse setting | API budget (default) | Setting |
  | --- | --- | --- |
  | `readonly` | 1 | fixed |
  | `max_execution_time` | 30 s | `XBRL_API_QUERY_TIMEOUT_SECONDS` |
  | `timeout_before_checking_execution_speed` | 0 | fixed |
  | `max_memory_usage` | 512 MiB | `XBRL_API_QUERY_MAX_MEMORY_BYTES` |
  | `max_rows_to_read` | 5,000,000 | `XBRL_API_QUERY_MAX_ROWS` |
  | `max_bytes_to_read` | 1 GiB | `XBRL_API_QUERY_MAX_BYTES` |
  | `max_result_rows` | 10,000 | fixed |
  | `max_result_bytes` | 16 MiB | `XBRL_API_RESULT_MAX_BYTES` |
  | `max_threads` | 2 | fixed |
  | `result_overflow_mode`, `read_overflow_mode`, `timeout_overflow_mode` | `throw` | fixed |

  Source: `ClickHouseWarehouse.__init__` (`read_only=True`) and `config.py`.
- **PostgreSQL API login.** The API needs a login with CONNECT and no table or writer
  privileges (`XBRL_API_POSTGRES_DSN`).
- **Provisioning.** `ops/provision/provision_identities.sh` creates or updates
  `xbrl_worker` and `xbrl_api` in PostgreSQL (`ops/provision/postgres_roles.sql`:
  CONNECT, DML grants plus default privileges for future tables, no `CREATE` on
  `public`) and `xbrl_writer`, `xbrl_api`, and `xbrl_api_profile` in ClickHouse. The
  profile is `readonly = 1` with every budget in the table above; under it the API
  connects without per-query settings and its startup check confirms the profile's
  values. `minio-init` creates the workers' object-store user and policy.
- **Grafana** in the monitoring profile allows anonymous Viewer access. That's for local use only.
- **Workers** have no inbound API. Their metrics port stays on the Compose network.
- **Filing archives.** A `-xbrl.zip` is checked against the filing budget
  (`XBRL_FILING_MAX_MEMBERS`, `XBRL_FILING_MAX_BYTES`) while its central directory is
  read, before any member is decompressed.

## Secrets

- `.env` and `.env.*` are git-ignored; only `.env.example` is tracked, and it holds
  development placeholders, never release credentials.
- Production secrets come from a secret manager or protected environment. Prometheus
  reads the API bearer token from a credentials file when auth is enabled.
- `minio-init` passes credentials as quoted environment values and fails closed,
  and the bucket is set to no anonymous access.

## Sensitive data

Filing data is public SEC data. The sensitive items are credentials, the operator's
SEC contact identity, and the integrity of stored artifacts and lineage.

## Abuse and resource limits

- Raw fact queries require a CIK, an accession, or a complete filing-date window of at
  most 366 days. Offsets are capped, accessions are syntax-checked, and per-process
  concurrency is bounded. ClickHouse queries carry time, memory, rows-read, and
  result-byte budgets.
- Filing keys (the `accession` path and query parameter, `FilingRef.accession`) must
  match `FILING_KEY_PATTERN` in `domain/sources.py` and be at most 128 characters: an
  SEC accession or `<source>-<native id>` of ASCII letters, digits, `.`, `_`, and `-`.
  The API rejects anything else with 422 before a query. A valid key has no quotes,
  spaces, slashes, or control characters, which is what lets `storage
  retire-revisions` format keys into its `ALTER TABLE ... DELETE` and the downloader
  put them in object keys. Keep that property if the pattern changes.
- Each API process runs one readiness probe at a time. Overlapping probes get 503.
- SEC requests use the configured user agent. They default to 8 req/s with jitter and
  honor `Retry-After`. **The limiter is process-local**, so a PostgreSQL advisory lock
  (`SecEgressLock`, see SEC_ACCESS.md) lets only one process send SEC traffic at a
  time. A second SEC-active process fails fast instead of doubling the rate. Other
  sources follow the same rule with their own policy rate and their own lock
  (`SourceEgressLock`, `xbrl:<source>-egress`), so the SEC sender and, say, an ESEF
  sender run side by side without sharing a limit.
- The `api`, `worker`, and `worker-parse` containers run the application image as
  UID/GID `10001:10001` with all capabilities dropped and `no-new-privileges`. The
  third-party storage and monitoring images keep their own defaults. Host ports bind
  to `127.0.0.1`.
- Disk reserve checks (`XBRL_DISK_RESERVE_BYTES`) pause claims and downloads
  when local space runs low.

## Dependencies

- Taxonomy packages are third-party files pinned by URL and SHA-256 in
  `config/taxonomy_packages.yml`. They are downloaded at deployment, never committed.
- `constraints-tested.txt` snapshots tested Python versions. It isn't a hash-locked,
  cross-platform lock.
- Third-party images are pinned by digest. The application image builds with
  npm for Arelle EFM's PythonMonkey dependency.
- Not yet established: OS-package and JavaScript dependency scanning, and a scan of
  a pinned release image. `pip-audit` isn't installed in the dev environment.

## Checks for security-sensitive behavior

Run from `sec-xbrl-warehouse/` with the virtualenv active:

```bash
pytest tests/unit/test_api.py tests/unit/test_config.py tests/unit/test_deployment.py tests/unit/test_bulk_security.py tests/unit/test_sec_client.py tests/unit/test_source_adapters.py tests/unit/test_esef_source.py tests/unit/test_edinet_dart_sources.py tests/unit/test_object_store.py
```

These cover API auth and capacity, production credential checks, API environment
isolation, bucket setup quoting, ZIP limits and unsafe names, SEC retry behavior,
per-source host allowlists, disjoint source hosts and redirect refusal, ESEF digest
checks, unsafe records and report package catalogs, EDINET/DART key redaction
(logs, errors, sidecars, stored objects) and missing-key refusal, and bounded object
restores. The full gates are listed in the root `AGENTS.md`.

## Before public exposure

The provisioning script grants `xbrl_api` `SELECT` on the whole warehouse database
(all public SEC data). Today `QueryService` reads only `v_xbrl_facts_latest`,
`v_canonical_facts_latest`, `metric_series`, and `companies`; narrow the grant to those
if direct ClickHouse readers shouldn't see the rest. Replace every development
password (`XBRL_ENV=production` refuses them) and supply them from a secret manager.


Public exposure also needs production authentication, a dedicated bounded ClickHouse
reader, TLS, managed secrets, synced taxonomy packages with offline parsing, private
service networks, a pinned and scanned release image, and a completed backup restore
drill. Remaining limits are tracked in `HANDOFF.md` and the repository's GitHub issues;
the audits that led here are in [history/](history/README.md).
