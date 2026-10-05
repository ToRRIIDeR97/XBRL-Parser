# Additional performance and security audit — 8 September 2026

Reviewed the current worktree, including uncommitted hardening changes and the CSV field-limit fix. Three general subagents reviewed API/query behavior, download/parser boundaries, and storage/recovery; the root agent reviewed deployment controls, checked findings, and reran the checkpoint and taxonomy probes. Application code and operational state weren't changed during this audit. The worker remains stopped.

P1 means address before the affected recovery or upgrade operation. P2 means address before the identified workload, scaling step, or wider exposure. P3 means measure and optimize after correctness controls are established. Priority reflects impact and prerequisites, not a claim that every issue is currently being triggered.

| Rank | Priority | Area | Finding | When it matters |
| --- | --- | --- | --- | --- |
| 1 | P1 | Recovery / data integrity | Changed batch boundaries can silently skip source rows on replay | Restart with different batch settings or parser behavior |
| 2 | P1 | Upgrade availability / integrity | Schema checks miss column-type and nullability changes | Upgrade an existing warehouse |
| 3 | P2 | Security | Entrypoint selection and taxonomy cache need complete path containment | Imported or tampered filing metadata; cache helper boundary |
| 4 | P2 | Recovery | Silver checkpoints do not verify their retained objects | Partial restore or missing/corrupt Parquet objects |
| 5 | P2 | Memory / availability | Some ingestion allocations occur outside byte-aware batching | Large text rows or canonical batches |
| 6 | P2 | Availability | A numeric Retry-After can occupy a worker for an unbounded interval | SEC/proxy throttling response with a very long delay |
| 7 | P2 | Security | API container receives unnecessary writer and storage credentials | Public/shared deployment or API process compromise |
| 8 | P2 | Supply chain / replay / build performance | Dependencies and images are not reproducibly locked | Rebuild, release, rollback, parser replay |
| 9 | P2 | Capacity / availability | Backfill lacks a disk reserve gate and complete host resource controls | Full historical queue and growing retained data |
| 10 | P2 | Availability / SEC access | Rate limits are independent across clients and processes | Concurrent discovery, parsers, or additional workers |
| 11 | P2 | Query performance | Accession-only queries lack a matching leading storage key | Large incremental XBRL history |
| 12 | P3 | Replay performance | Checkpoints avoid writes but still reparse completed batches | Repeated late-stage failures in large archives |

## 1. Changed batch boundaries can silently skip source rows

The bulk loader identifies a checkpoint by artifact, source revision, table, and batch index. Its payload contains row count and object keys, without the source range, content identity, or reader settings. A different batch size therefore assigns different source rows to the same checkpoint key. The loader also derives its expected reconciliation count from these saved counts. See [bulk_loader.py:207](../../src/sec_xbrl_warehouse/ingestion/bulk_loader.py#L207) and [sec_tsv.py:224](../../src/sec_xbrl_warehouse/ingestion/sec_tsv.py#L224).

**Reproduced independently:** a three-row TAG fixture first checkpointed `Revenues` using one-row batches, then failed. Restarting with two-row batches skipped `Revenues` plus `Assets`, wrote `Liabilities`, and reported two rows. Stored rows and the reported count agreed, while `Assets` was missing from the three-row source. This used temporary ZIPs and fake storage, not the live warehouse.

**Action:** record and validate the source checksum, parser/schema identity, batch budgets, and source range or digest before reusing a checkpoint. Fail closed on incompatibility; use an explicit compatible replay or a new extraction identity. Reconcile against an independently established source count. Until then, preserve the exact batch budgets when resuming existing checkpoints. The CSV ceiling change itself didn't change those budgets; this audit doesn't establish missing rows in the current canary.

## 2. Schema checks miss column-type and nullability changes

[schema_drift](../../src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py#L135) compares sorting keys only. Both initialization and the preserving upgrade depend on this result. Existing tables with old `FixedString(32)` dimension hashes or nonnullable columns can be reported current even though the new DDL requires `String` or nullable values. This is directly relevant to the SEC drift recorded in the latest handover. The handover reports manual preserving swaps for the local dimension-hash tables; the general upgrade path remains incomplete.

The [copy path](../../src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py#L184) also selects only common columns, with one special backfill for `value_kind`, and checks row counts. Future added columns need explicit migration semantics so implicit defaults don't masquerade as preserved values.

**Action:** compare column types, nullability, and required defaults using `system.columns`; add preserving migrations for actual differences and reject unhandled conversions. Test an old populated schema upgrading to the current one, checking values as well as counts.

## 3. Complete the filesystem trust boundaries

Package member paths are contained, but [materialize_package](../../src/sec_xbrl_warehouse/ingestion/filing_downloader.py#L265) returns `destination / _choose_entrypoint(manifest)`. [Entrypoint selection](../../src/sec_xbrl_warehouse/ingestion/filing_downloader.py#L390) accepts `instance_document` and `primary_document` verbatim; [FilingRef](../../src/sec_xbrl_warehouse/domain/models.py#L81) doesn't constrain them. An absolute value overrides the destination, and a parent-relative value can escape it. This requires crafted or corrupted filing/job/checkpoint metadata; no public API upload path was demonstrated.

Separately, the [taxonomy cache lookup](../../src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py#L777) returns a mapped cache path after only `is_file()`, without resolving and checking its cache root. The root agent reproduced the helper returning a harmless outside file using the real Arelle WebCache with offline mode enabled. **Reachability caveat:** normal Arelle `ModelDocument.load` normalizes the tested URL first, and that tested traversal is then rejected by the hostname check. This is a confirmed helper containment gap, not a demonstrated arbitrary-file-read exploit through a complete malicious filing.

**Action:** validate entrypoint names, require the selected document to be a materialized manifest member, and check the resolved final path. Resolve and contain every cache candidate and returned cache path, including redirects and symlinks, before reading or writing.

## 4. Silver checkpoints do not verify their retained objects

[Bulk replay](../../src/sec_xbrl_warehouse/ingestion/bulk_loader.py#L211) trusts saved Parquet keys. [Incremental persistence](../../src/sec_xbrl_warehouse/ingestion/incremental.py#L230) skips an entire silver entity when its checkpoint exists. [Bulk reconciliation](../../src/sec_xbrl_warehouse/validation/data_quality.py#L79) queries ClickHouse; it doesn't read or validate silver objects.

A PostgreSQL restore with incomplete object storage, or subsequent object loss, can leave checkpoints that imply completion without a complete rebuild source. No missing live object was established in this audit.

**Action:** verify checkpoint object identity, existence, size/checksum, and row counts before trusting recovered checkpoints. Rebuild missing silver output from retained bronze and reconcile it. An object HEAD alone doesn't establish content integrity.

## 5. Memory budgets do not cover every allocation

The [TSV reader](../../src/sec_xbrl_warehouse/ingestion/sec_tsv.py#L215) passes a TextIOWrapper directly to DictReader. The text iterator materializes a physical line before CSV field validation, and the row byte check runs after parsing and normalization. A malformed very long line, including many individually permitted fields, can consume substantially more memory than `batch_bytes` before rejection. The fixed CSV character ceiling therefore doesn't provide a physical-line byte ceiling.

[Canonical mapping](../../src/sec_xbrl_warehouse/mapping/canonical_mapper.py#L229) batches by 500 source facts, and [canonical insertion](../../src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py#L321) builds the entire row list and Arrow table without applying `arrow_batch_bytes`. Large text values and conversion copies can increase peak memory. Existing parser-process and container limits contain some impact, but don't make these allocations byte-aware. No production peak-RSS benchmark was run.

**Action:** bound physical input-line reads before decoding/parsing and retain member/line diagnostics on rejection. Apply byte-aware batching to canonical conversion and insertion, including a single-record ceiling. Measure peak RSS on large real text facts before selecting final budgets.

## 6. Retry-After can stall one worker for an unbounded interval

[SecHttpClient._request](../../src/sec_xbrl_warehouse/infrastructure/http/sec_client.py#L125) passes an all-digit `Retry-After` directly to `asyncio.sleep`; only the fallback exponential delay is capped. A subagent mock-transport probe supplied `999999999` and observed that exact sleep request. No actual wait or SEC request was required. The worker can keep heartbeating while making no queue progress.

**Action:** bound in-process waiting and the overall request/job deadline. Persist a deferred retry time for long server delays, honoring the requested backoff without holding the worker. Don't simply retry earlier than the server requested. Handle HTTP-date and invalid values deliberately.

## 7. Reduce API credential privileges

The [API Compose service](../../docker-compose.yml#L88) loads the same `.env` used by the worker and explicitly receives the control-database bootstrap identity. The template also supplies writer ClickHouse and MinIO root credentials. The API uses a dedicated ClickHouse reader when configured, but those other credentials still exist in its environment. The API routes don't need object-store administration.

**Action:** separate service environments and identities. Give the API only the required database read/health permissions, remove writer and object-store credentials, and keep bootstrap/migration secrets out of runtime services. This reduces the consequences of an API compromise; no compromise or remote exposure was demonstrated.

## 8. Make runtime rebuilds reproducible

[Python dependencies](../../pyproject.toml#L15) use ranges without a transitive lock, and [container images](../../docker-compose.yml#L5) use mutable tags. [Dockerfile](../../docker/app.Dockerfile#L20) copies application source before installing npm and resolving Python dependencies, so even a parser-only edit repeats this expensive layer and can select different library versions. CI also installs from ranges.

**Action:** lock resolved dependencies and retain release image digests plus Arelle/parser versions in replay metadata. Cache third-party installation separately from application source. Scan the final image's Python, OS, and JavaScript dependencies. This audit didn't rerun a vulnerability database or image scan and makes no new CVE claim.

## 9. Add backfill disk and host capacity controls

[Doctor](../../src/sec_xbrl_warehouse/cli.py#L89) prints free space but passes the data-directory check based on writability alone. Normal ingestion lacks a minimum-free-space gate. [Bulk restore staging](../../src/sec_xbrl_warehouse/ingestion/orchestrator.py#L181) uses the system temporary directory, while Parquet staging uses the configured temporary directory. Both need capacity checks. Source retention, silver output, ClickHouse merges, and preserving migrations all increase disk demand.

Compose bounds API/worker memory but doesn't define memory/CPU budgets for the data services or log rotation. The [bundled Prometheus configuration](../../ops/prometheus.yml#L1) contains scrape targets but no disk alert rules. Docker-daemon or external monitoring controls may exist; they weren't inspected.

**Action:** check the actual scratch and data volumes before claiming or downloading large jobs, pause at a reserve threshold, size data-service resources, rotate logs, and route disk/queue alerts. Preserve raw objects; this isn't a recommendation for automatic source deletion.

## 10. Coordinate SEC traffic before adding concurrency

Each [SecHttpClient](../../src/sec_xbrl_warehouse/infrastructure/http/sec_client.py#L33) constructs its own limiter, while [Arelle taxonomy access](../../src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py#L675) uses an independent schedule. Concurrent discovery, worker replicas, or parser processes can exceed the intended aggregate request rate despite individually valid configurations. This remains a documented scaling limitation.

**Action:** use a shared egress budget or strictly schedule clients so their combined rates stay within the configured policy. Keep one canary worker until coordination exists. No SEC traffic was generated during this audit.

## 11. Measure and improve accession-only query access

[Filing summaries](../../src/sec_xbrl_warehouse/query/service.py#L194) filter by accession and compute a count plus two `uniqExact` aggregates. Raw queries also accept accession alone. The [fact table](../../sql/clickhouse/020_full_xbrl_tables.sql#L99) is partitioned by year and sorted first by CIK/concept/period, with accession later. Accession-only lookups lack matching leading-key constraints and can read broad portions of history, reaching the existing per-query budgets and returning 503 as data grows.

**Action:** validate filing-route accession syntax, use known CIK/date metadata to narrow reads, then measure `EXPLAIN` and rows/bytes read on representative data before adding an accession projection or precomputed summary. This is a code-supported access-pattern risk, not a measured production latency result. Preserve current query budgets and same-revision duplicate handling.

## 12. Avoid repeating completed parsing work on replay

The [bulk loader](../../src/sec_xbrl_warehouse/ingestion/bulk_loader.py#L209) constructs the next Arrow batch before looking up its checkpoint. Restore and ZIP validation also precede replay. Checkpoints therefore save downstream writes, but a late failure still repeats substantial object transfer, decompression, parsing, and allocation work for completed stages.

**Action:** after fixing checkpoint compatibility and validating retained silver output, resume from verified table/stage manifests or retained Parquet where possible. Measure the cost first. Keep source checksum/CRC guarantees rather than removing validation without a trusted replacement.

## Verification and remaining limits

- Root run: `PYTHONPATH` is supplied by the test configuration; `.venv/bin/python -m pytest -m 'not integration' -q` returned **166 passed, 16 deselected**, with two existing deprecation warnings.
- Root independently reproduced the changed-batch checkpoint omission with temporary ZIPs/fake stores and the cache helper escape with a harmless temporary file, real WebCache, and offline mode.
- API subagent: seven API tests passed; targeted Ruff passed. Parser subagent used local path and mock HTTP probes. Storage subagent used the temporary replay probe.
- `git diff --check` passed before adding this report. No application source was edited during the audit, no live data was written, and no worker was restarted. The earlier image build completed without restarting a service.
- No full storage integration suite, live SEC canary, stress/RSS benchmark, representative query-plan study, fresh vulnerability scan, penetration test, or restore drill was performed.
- Independent PostgreSQL/object-store backups and a successful isolated restore remain acceptance requirements from the handover and backup runbook. Their absence from this audit is an evidence gap, not proof that external backups don't exist.
- Earlier protections remain in place: bounded API queries and concurrency, production API authentication requirements, HTTPS host restrictions, immutable object writes, checkpoint lease fencing, parser isolation, and raw retention. They should be preserved during remediation.

For the paused July canary, first establish checkpoint compatibility and record the exact image, schema, and batch settings. Keep its 43 saved batches intact. Broader queue resumption should follow a completed and reconciled canary, not this source audit alone.
