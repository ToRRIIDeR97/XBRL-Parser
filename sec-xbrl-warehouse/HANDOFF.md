# Engineering Handoff

## Status update

All implementation and hardening points across Milestones 1-5 have been
implemented and covered by unit/integration tests. See `FUNCTION_STUBS.md`
(0 implementation points remain) and `BUILD_REPORT.md`. The remaining work
described below is the production backfill itself: downloading real SEC data,
processing the acceptance corpus of real filings, and operational hardening
(rate-limit tuning, runbook drills, backup/restore exercises).

This document tells the next coding agent exactly where to work, what each file owns, and how each implementation is accepted.

Search the repository for `IMPLEMENT_ME`. The stubs are deliberately executable contracts rather than vague TODO comments. `FUNCTION_STUBS.md` provides the generated symbol/signature index.

## Milestone 0 — Verify the scaffold

Run:

```bash
cp .env.example .env
# Replace SEC_USER_AGENT before making SEC requests.
docker compose up -d postgres clickhouse minio minio-init
python -m pip install -e ".[dev]"
alembic upgrade head
xbrl-warehouse db init-clickhouse
xbrl-warehouse doctor
pytest -m "not integration"
```

Do not begin large downloads until the database schemas, object-store bucket, and local disk paths pass `doctor`. Review `config/taxonomy_packages.yml` and create the configured Arelle cache/package directories before filing-level parsing.

## Milestone 1 — Historical FSNDS backfill

### `src/sec_xbrl_warehouse/infrastructure/storage/postgres.py`

Implement and test:

- `PostgresControlRepository.upsert_source_artifact`
- `PostgresControlRepository.mark_artifact_state`
- `PostgresControlRepository.enqueue_job`
- `PostgresControlRepository.claim_job`
- `PostgresControlRepository.complete_job`
- `PostgresControlRepository.fail_job`
- `PostgresControlRepository.upsert_filing`
- `PostgresControlRepository.record_validation_issues`

Required behavior:

- URL and checksum are idempotency keys.
- Job claiming uses one transaction and `FOR UPDATE SKIP LOCKED`.
- A worker crash allows a stale lock to be reclaimed.
- State transitions reject impossible regressions.
- Error text is bounded; full diagnostics go to object storage.

Acceptance:

- Two concurrent workers never claim the same job.
- Re-enqueuing the same idempotency key returns the existing job.
- Integration tests cover success, retry, dead-letter, and stale-lock recovery.

### `src/sec_xbrl_warehouse/infrastructure/storage/arrow_schemas.py`

Treat the checked-in Arrow schemas as versioned ingestion contracts. Complete the source-to-warehouse conversion path so every batch exactly matches:

- `sec_source_schema` before lexical coercion;
- `sec_warehouse_schema` before the eight SEC table inserts;
- `xbrl_warehouse_schema` before full-fidelity Arelle entity inserts;
- `canonical_fact_schema` before canonical writes.

Acceptance:

- schema names, order, types, nullability, decimal precision/scale, and lineage columns match the ClickHouse DDL;
- schema evolution is explicit and documented rather than silently cast;
- negative tests prove reordered, missing, and type-incompatible batches fail before network/database writes.

### `src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py`

Complete specialized insert methods:

- `insert_sec_submissions`
- `insert_sec_tags`
- `insert_sec_dimensions`
- `insert_sec_numeric_facts`
- `insert_sec_text_facts`
- `insert_sec_renderings`
- `insert_sec_presentations`
- `insert_sec_calculations`
- `insert_xbrl_bundle`
- `insert_canonical_facts`

Required behavior:

- Accept Arrow `RecordBatch`/`Table`, not Python row-by-row inserts.
- Validate the expected column order and types before sending.
- Add ingest metadata consistently.
- Use bounded batch sizes.
- Replaying the same artifact/accession produces the same logical state through the ReplacingMergeTree version column.

Acceptance:

- Each synthetic FSNDS table loads.
- Row counts reconcile to the source fixture.
- A second load does not change deduplicated query counts.

### `src/sec_xbrl_warehouse/ingestion/sec_tsv.py`

Finish production hardening around:

- `SecTsvBatchReader.iter_batches`
- `SecTsvBatchReader.validate_manifest`
- `SecTsvBatchReader.validate_header`
- `coerce_sec_batch`

Required behavior:

- Stream directly from ZIP members.
- Never extract the full archive merely to parse it.
- Detect BOM, encoding errors, missing files, duplicate files, unknown columns, and changed column order.
- Preserve unexpected columns in quarantine metadata.
- Parse decimals without binary floating-point loss.

Acceptance:

- Peak memory remains bounded while reading a generated multi-gigabyte fixture.
- Header drift fails before warehouse writes begin.
- A malformed row identifies table, batch, and source line/offset where available.

### `src/sec_xbrl_warehouse/ingestion/bulk_loader.py`

Implement:

- `BulkArtifactLoader.load_artifact`
- `BulkArtifactLoader._load_table`
- `BulkArtifactLoader._write_silver_parquet`
- `BulkArtifactLoader._write_clickhouse`
- `BulkArtifactLoader._reconcile`

Required stage order:

1. Verify source checksum and ZIP integrity.
2. Validate complete manifest and all headers.
3. Stream each table in dependency order: `SUB`, `TAG`, `DIM`, `REN`, `NUM`, `TXT`, `PRE`, `CAL`.
4. Write partitioned Parquet under the immutable artifact revision.
5. Write ClickHouse batches.
6. Run row-count, uniqueness, and reference checks.
7. Mark the artifact complete only after reconciliation.

Acceptance:

- Interrupting after any batch can be resumed without duplicate logical rows.
- A failed validation leaves the source artifact and diagnostics available in `quarantine/`.

### `src/sec_xbrl_warehouse/ingestion/orchestrator.py`

Implement:

- `IngestionOrchestrator.plan_backfill`
- `IngestionOrchestrator.enqueue_backfill`
- `IngestionOrchestrator.run_artifact_job`
- `IngestionOrchestrator.reconcile_artifact`

Acceptance:

- `xbrl-warehouse backfill plan` produces a stable semantic checksum for the same catalog snapshot and selection; only `generated_at` may differ.
- `backfill enqueue` creates one job per source revision.
- Re-running the command adds only newly published or changed SEC artifacts.

## Milestone 2 — Full-fidelity Arelle extraction

### `src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py`

Implement:

- `extract`
- `_create_controller`
- `_load_model`
- `_extract_document_metadata`
- `_extract_contexts`
- `_extract_concepts`
- `_extract_facts`
- `_extract_relationships`
- `_extract_footnotes`
- `_collect_validation_issues`

The adapter must emit the domain objects in `domain/xbrl.py` and must not leak Arelle objects outside the adapter. It must honor `XBRL_ARELLE_CACHE_DIR`, `XBRL_ARELLE_TAXONOMY_PACKAGES_DIR`, `XBRL_ARELLE_OFFLINE`, the allowed-host policy, and checksum-pinned entries in `config/taxonomy_packages.yml`.

Fact requirements:

- concept namespace, local name, QName, and taxonomy version;
- raw lexical value and typed numeric/text value;
- nil state, decimals, precision, language;
- context ID, entity scheme/identifier, period, scenario/segment dimensions;
- unit numerator/denominator measures;
- tuple ancestry where present;
- source document and inline fact ID;
- duplicate-set identity;
- footnote links.

Relationship requirements:

- presentation, calculation, definition, label, reference, and footnote arcs where available;
- role URI, arcrole URI, order, priority, preferred label, weight, usable/closed flags;
- from/to QName and network identity.

Acceptance corpus:

- `tests/fixtures/xbrl/simple/simple.xml`;
- at least ten real 10-K/10-Q filings;
- one multi-document inline XBRL filing;
- one IFRS foreign private issuer;
- one filing with extension dimensions;
- one amended filing;
- one filing with duplicate numeric facts.

### `src/sec_xbrl_warehouse/validation/xbrl_validation.py`

Normalize Arelle messages into `ValidationIssue` without discarding original message codes or locations.

Acceptance:

- A known-invalid synthetic fixture produces stable issue codes.
- Validation warnings do not automatically reject ingestion unless configured in `config/validation.yml`.

## Milestone 3 — Incremental EDGAR ingestion

### `src/sec_xbrl_warehouse/ingestion/filing_discovery.py`

Implement:

- `SecSubmissionsDiscovery.discover_company_filings`
- `SecDailyIndexDiscovery.discover_date`
- `SecDailyIndexDiscovery.discover_range`
- `merge_discovery_results`

Required behavior:

- Prefer daily/global indexes for universe-wide polling.
- Use submissions JSON for company-specific repair/reconciliation.
- Normalize accession numbers with dashes.
- Preserve amendment status and primary-document metadata.

### `src/sec_xbrl_warehouse/ingestion/filing_downloader.py`

Implement and harden:

- `FilingPackageDownloader.fetch_index`
- `FilingPackageDownloader.select_xbrl_entrypoints`
- `FilingPackageDownloader.download_package`
- `FilingPackageDownloader.archive_package`
- `FilingPackageDownloader.materialize_package`

Acceptance:

- Multi-document inline XBRL packages are retained intact.
- The package manifest records every downloaded file, checksum, MIME type, SEC URL, manifest object/checksum, and final archive object/checksum.
- Re-downloading an unchanged package performs no network transfer when the object already exists.

### `src/sec_xbrl_warehouse/ingestion/incremental.py`

Implement:

- `IncrementalFilingIngestor.ingest`
- `_persist_bundle`
- `_reconcile_with_sec_bulk`

Acceptance:

- A filing can be processed from discovery through ClickHouse using one accession command.
- The job can resume after download, parsing, Parquet, or ClickHouse failure.
- When the same filing later appears in an SEC bulk set, reconciliation records equivalence or discrepancies rather than overwriting one source with the other.

## Milestone 4 — Canonical metrics

### `src/sec_xbrl_warehouse/mapping/canonical_mapper.py`

Implement:

- `CanonicalMapper.load_rules`
- `CanonicalMapper.map_fact`
- `CanonicalMapper.map_batch`
- `CanonicalMapper.explain_mapping`
- `CanonicalMapper.enqueue_review`

Mapping precedence:

1. Explicit approved concept/version mapping.
2. Approved namespace + concept mapping with validity dates.
3. Filing presentation/calculation context and statement placement.
4. Configured candidate rules.
5. No mapping; retain raw fact and create review candidate.

Never map solely from a fuzzy label match without an approval/review state.

Acceptance:

- Every canonical fact has raw fact ID, mapping ID, mapping version, confidence, and explanation.
- Mapping changes are append-only/versioned so historical outputs can be reproduced.

## Milestone 5 — Query API and operations

### `src/sec_xbrl_warehouse/query/service.py`

Complete query methods and pagination. Protect ClickHouse from unbounded scans by requiring date/company/concept constraints for large endpoints.

### `src/sec_xbrl_warehouse/api/routers/*.py`

Replace placeholder responses with query-service calls, add request validation, and expose lineage fields.

### `src/sec_xbrl_warehouse/worker.py`

Implement job dispatch, heartbeats, graceful shutdown, stale-lock recovery, and metrics.

### `src/sec_xbrl_warehouse/observability/metrics.py`

Instrument:

- source bytes downloaded;
- rows and facts parsed/written;
- parse/write latency;
- SEC response status and throttling;
- jobs by status;
- validation issues by severity/code;
- schema drift;
- reconciliation differences.

## Definition of done for the first production backfill

- 2016 through the latest complete SEC period loads from FSNDS.
- All eight source tables have source-to-warehouse row reconciliation.
- Source ZIPs and silver Parquet are checksum-addressed and retained.
- Backfill is restartable after process or machine failure.
- Unknown schema changes stop only the affected artifact and emit actionable diagnostics.
- Query examples in `sql/clickhouse/queries/` return expected results.
- A sample of at least 100 filings is independently re-parsed with Arelle and compared against the bulk tables.
- Runbook includes backup, restore, upgrade, and data-rebuild procedures.
