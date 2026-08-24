# Project Structure and Setup Inventory

This file is the navigation and implementation inventory for the SEC XBRL Warehouse scaffold. Every checked-in file is listed below with its ownership boundary. The coding agent should read `AGENTS.md`, `HANDOFF.md`, `docs/ARCHITECTURE.md`, and `docs/DATA_MODEL.md` before changing code.

## Required setup

1. Install Python 3.12 and Docker with Compose v2.
2. Copy `.env.example` to `.env` and replace the SEC identity placeholder with a real name or organization plus a monitored email.
3. Run `scripts/bootstrap.sh` on macOS/Linux or `scripts/bootstrap.ps1` on Windows.
4. Confirm PostgreSQL, ClickHouse, and MinIO pass `xbrl-warehouse doctor`.
5. Run unit tests and scan `IMPLEMENT_ME` markers before beginning a milestone.
6. Implement milestones in `HANDOFF.md` order; do not begin broad SEC downloads before idempotency and reconciliation tests pass.

## Runtime topology

| Concern | Component | Source of truth |
|---|---|---|
| Historical source | SEC Financial Statement and Notes bulk ZIPs | Immutable bronze object |
| Incremental source | EDGAR accession package | Immutable bronze package/manifest |
| XBRL semantics | Arelle adapter | Raw extraction bundle |
| Control state | PostgreSQL | Artifacts, filings, jobs, issues, mappings |
| Analytical facts | ClickHouse | Rebuildable from silver/bronze |
| Durable files | S3/MinIO or local object store | ZIPs, package members, manifests, Parquet |
| Query surface | FastAPI/CLI | Bounded parameterized services |

## Full file inventory

### Root and environment

- `.dockerignore` — Excludes local data, virtual environments, caches, and secrets from application image builds.
- `.editorconfig` — Cross-editor newline, whitespace, and indentation defaults.
- `.env.example` — Complete environment-variable template for SEC identity, services, parsing, worker, and API settings.
- `.gitignore` — Prevents secrets, databases, caches, generated data, and build outputs from entering Git.
- `.pre-commit-config.yaml` — Ruff and repository-hygiene hooks run before commits.
- `.python-version` — Declares Python 3.12 for pyenv-compatible tooling.
- `AGENTS.md` — Non-negotiable implementation and validation contract for the coding agent taking over.
- `BUILD_REPORT.md` — Delivered scope, local validation results, skipped checks, and intentional unfinished surfaces.
- `HANDOFF.md` — Milestone-by-milestone list of stubs, expected behavior, and acceptance tests.
- `FUNCTION_STUBS.md` — Generated symbol-level index of implementation points, signatures, and protocol contracts.
- `LICENSE` — MIT license for the scaffold.
- `Makefile` — Common local commands for install, services, schemas, tests, formatting, typing, and stub scanning.
- `MANIFEST.sha256` — Generated SHA-256 digest list for every packaged repository file except itself.
- `PROJECT_STRUCTURE.md` — This complete file inventory and setup map.
- `PROJECT_TREE.txt` — Generated plain-text repository tree for rapid navigation.
- `README.md` — System overview, quick start, architecture, commands, guarantees, and scaffold status.
- `alembic.ini` — Alembic configuration for the PostgreSQL control-plane migrations.
- `docker-compose.yml` — Local PostgreSQL, ClickHouse, MinIO, API, and worker service topology.
- `pyproject.toml` — Python package metadata, dependencies, CLI entry point, and Ruff/mypy/pytest/coverage settings.

### Continuous integration

- `.github/workflows/ci.yml` — Unit/coverage/static-analysis and PostgreSQL/ClickHouse integration workflow for Python 3.12.

### Configuration

- `config/canonical_metrics.yml` — Stable canonical metric definitions and derived-metric formulas.
- `config/concept_seed.yml` — Unapproved US-GAAP concept candidates used to populate the mapping review workflow.
- `config/logging.yml` — Reference structured-logging configuration and field policy.
- `config/sources.yml` — SEC catalog/API/archive endpoints, supported forms, exclusions, and object-key layout templates.
- `config/taxonomy_packages.yml` — Registry and network policy for checksum-pinned Arelle taxonomy packages.
- `config/validation.yml` — Schema-drift, XBRL validation, data-quality, and reconciliation thresholds.

### Runtime data placeholder

- `data/.gitkeep` — Keeps the runtime data directory in Git while ignoring its generated contents.

### Containers

- `docker/app.Dockerfile` — Python 3.12 runtime image with native XML build dependencies and installed application.
- `docker/entrypoint.sh` — Creates runtime directories and dispatches the CLI inside the container.

### PostgreSQL migrations

- `migrations/env.py` — Async Alembic environment wired to application settings and SQLAlchemy metadata.
- `migrations/script.py.mako` — Template used when generating future Alembic revisions.
- `migrations/versions/0001_control_plane.py` — Initial PostgreSQL schema for artifacts, filings, queue/checkpoints, validation, runs, metrics, mappings, and reviews.

### ClickHouse schemas and queries

- `sql/clickhouse/001_database.sql` — Creates the analytical database.
- `sql/clickhouse/010_sec_bulk_tables.sql` — Creates the eight flattened SEC FSNDS warehouse tables with source revision lineage.
- `sql/clickhouse/020_full_xbrl_tables.sql` — Creates full-fidelity contexts, concepts, facts, relationships, and footnotes tables.
- `sql/clickhouse/030_canonical_tables.sql` — Creates versioned canonical facts with raw-fact and mapping lineage.
- `sql/clickhouse/040_views.sql` — Provides latest-state convenience views; production queries should assess FINAL cost.
- `sql/clickhouse/queries/company_revenue_history.sql` — Reference query for canonical company revenue history.
- `sql/clickhouse/queries/raw_concept_history.sql` — Reference query for raw as-filed concept history.
- `sql/clickhouse/queries/reconcile_artifact_counts.sql` — Reference artifact row-count reconciliation query.

### Application source

- `src/sec_xbrl_warehouse/__init__.py` — Package metadata and version.
- `src/sec_xbrl_warehouse/__main__.py` — Allows `python -m sec_xbrl_warehouse` to launch the CLI.
- `src/sec_xbrl_warehouse/api/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/api/app.py` — FastAPI lifecycle, shared storage clients, routers, and Prometheus mount.
- `src/sec_xbrl_warehouse/api/dependencies.py` — FastAPI dependency accessors for storage and query services.
- `src/sec_xbrl_warehouse/api/routers/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/api/routers/facts.py` — Raw fact and canonical series endpoints with bounded filters.
- `src/sec_xbrl_warehouse/api/routers/filings.py` — Filing summary endpoint.
- `src/sec_xbrl_warehouse/api/routers/health.py` — Liveness and PostgreSQL/ClickHouse readiness endpoints.
- `src/sec_xbrl_warehouse/cli.py` — Typer command tree for doctor, schemas, catalog, plans, queue, API, and worker.
- `src/sec_xbrl_warehouse/config.py` — Typed environment settings, SEC identity guard, limits, credentials, and runtime paths.
- `src/sec_xbrl_warehouse/config_files.py` — Strict YAML configuration loader.
- `src/sec_xbrl_warehouse/domain/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/domain/canonical.py` — Immutable canonical metric, mapping, and canonical fact contracts.
- `src/sec_xbrl_warehouse/domain/enums.py` — Dataset, table, lifecycle, job, severity, period, fact, relationship, and statement enumerations.
- `src/sec_xbrl_warehouse/domain/models.py` — Immutable source artifact, filing/package, load, validation, job, and backfill-plan models.
- `src/sec_xbrl_warehouse/domain/ports.py` — Protocols isolating object storage, PostgreSQL, ClickHouse, extraction, discovery, download, and batch writing.
- `src/sec_xbrl_warehouse/domain/sec_fsnds.py` — Typed row models for SEC SUB, TAG, DIM, NUM, TXT, REN, PRE, and CAL data.
- `src/sec_xbrl_warehouse/domain/xbrl.py` — Full-fidelity context, unit, concept, fact, relationship, footnote, metadata, and bundle models.
- `src/sec_xbrl_warehouse/exceptions.py` — Typed application exceptions for configuration, access, integrity, drift, extraction, quality, state, and writes.
- `src/sec_xbrl_warehouse/infrastructure/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/infrastructure/http/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/infrastructure/http/rate_limiter.py` — Process-local evenly spaced SEC request limiter.
- `src/sec_xbrl_warehouse/infrastructure/http/sec_client.py` — SEC-only HTTP client with identity, host allow-list, throttling, retries, streaming, and checksums.
- `src/sec_xbrl_warehouse/infrastructure/storage/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/infrastructure/storage/arrow_schemas.py` — Exact source, SEC warehouse, full-XBRL, and canonical PyArrow schema contracts.
- `src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py` — Arrow-first ClickHouse DDL/query/insert adapter and conversion stubs.
- `src/sec_xbrl_warehouse/infrastructure/storage/hashing.py` — Streaming SHA-256 helpers for files and bytes.
- `src/sec_xbrl_warehouse/infrastructure/storage/object_store.py` — Immutable local and S3/MinIO object-store implementations.
- `src/sec_xbrl_warehouse/infrastructure/storage/parquet.py` — Partitioned immutable Parquet batch writer.
- `src/sec_xbrl_warehouse/infrastructure/storage/postgres.py` — Async transactional control repository with artifact versions and SKIP LOCKED queue behavior.
- `src/sec_xbrl_warehouse/infrastructure/storage/postgres_models.py` — SQLAlchemy 2.0 ORM definitions mirroring the initial control-plane migration.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py` — Arelle extraction boundary, detailed helper stubs, and deterministic semantic helpers.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/edgartools_adapter.py` — Optional EdgarTools discovery convenience adapter that cannot replace Arelle.
- `src/sec_xbrl_warehouse/ingestion/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/ingestion/bulk_catalog.py` — SEC bulk catalog parser, period/size normalization, and year filtering.
- `src/sec_xbrl_warehouse/ingestion/bulk_downloader.py` — Bulk ZIP acquisition, integrity checks, and immutable bronze keying.
- `src/sec_xbrl_warehouse/ingestion/bulk_loader.py` — Restartable historical load orchestration and reconciliation stubs.
- `src/sec_xbrl_warehouse/ingestion/filing_discovery.py` — SEC submissions parser plus issuer and daily-index discovery boundaries.
- `src/sec_xbrl_warehouse/ingestion/filing_downloader.py` — Accession package selection, immutable member storage, manifesting, and materialization stub.
- `src/sec_xbrl_warehouse/ingestion/incremental.py` — Accession-level package-to-Arelle-to-Parquet-to-ClickHouse pipeline contract.
- `src/sec_xbrl_warehouse/ingestion/orchestrator.py` — Deterministic backfill plan creation and idempotent job enqueueing.
- `src/sec_xbrl_warehouse/ingestion/sec_tsv.py` — Bounded ZIP member reader, exact eight-table headers, drift checks, and Arrow typing stub.
- `src/sec_xbrl_warehouse/logging.py` — Structured logging configuration with a stdlib fallback for lightweight tests.
- `src/sec_xbrl_warehouse/mapping/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/mapping/canonical_mapper.py` — Versioned mapping precedence, candidate review, explanation, and canonical fact construction.
- `src/sec_xbrl_warehouse/normalization/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/normalization/concepts.py` — Concept identity, namespace-family hints, and display-QName helpers.
- `src/sec_xbrl_warehouse/normalization/dimensions.py` — Deterministic dimension ordering, JSON serialization, hashing, and consolidated-context hint.
- `src/sec_xbrl_warehouse/normalization/facts.py` — Non-destructive numeric/unit normalization and reconciliation keys.
- `src/sec_xbrl_warehouse/normalization/periods.py` — Period validation, inclusive duration, and conservative quarter-count hints.
- `src/sec_xbrl_warehouse/observability/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/observability/metrics.py` — Prometheus counters, gauges, and histograms plus SEC hook stub.
- `src/sec_xbrl_warehouse/query/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/query/service.py` — Bounded, parameterized raw/canonical fact and filing summary queries.
- `src/sec_xbrl_warehouse/validation/__init__.py` — Declares the package boundary and its public module purpose.
- `src/sec_xbrl_warehouse/validation/data_quality.py` — Quality result types, row/rate checks, failure aggregation, and artifact reconciler stub.
- `src/sec_xbrl_warehouse/validation/schema_drift.py` — Header comparison and fail-fast exact schema enforcement.
- `src/sec_xbrl_warehouse/validation/xbrl_validation.py` — Fatal issue rejection and stable issue summaries.
- `src/sec_xbrl_warehouse/worker.py` — Queue claim/heartbeat/dispatch/retry loop and handler wiring boundary.

### Operator and developer scripts

- `scripts/bootstrap.ps1` — Equivalent Windows PowerShell bootstrap.
- `scripts/bootstrap.sh` — One-command macOS/Linux virtual environment, dependency, Docker, migration, and schema bootstrap.
- `scripts/build_fixture_zip.py` — Regenerates the eight-table synthetic FSNDS ZIP without external dependencies.
- `scripts/download_sample_filing.py` — Small authenticated SEC discovery smoke test for a CIK and form.
- `scripts/generate_project_tree.py` — Regenerates PROJECT_TREE.txt from the live repository.
- `scripts/generate_stub_index.py` — Regenerates FUNCTION_STUBS.md from source symbols and Protocol interfaces.
- `scripts/scan_stubs.py` — Lists every IMPLEMENT_ME marker with file and line number.
- `scripts/verify_environment.py` — Checks Python, Docker, package versions, and required repository directories.

### Tests and fixtures

- `tests/conftest.py` — Adds the src layout for tests and supplies safe test settings.
- `tests/fixtures/README.md` — Explains fixture purpose and regeneration policy.
- `tests/fixtures/catalog_notes_sample.html` — Stable catalog markup sample for link/period/size parsing.
- `tests/fixtures/fsnds-mini.zip` — One-row-per-table synthetic SEC bulk archive.
- `tests/fixtures/submissions_sample.json` — Synthetic columnar SEC submissions response.
- `tests/fixtures/xbrl/simple/simple-label.xml` — Synthetic label linkbase for future Arelle extraction tests.
- `tests/fixtures/xbrl/simple/simple.xml` — Synthetic conventional XBRL instance with context, unit, and revenue fact.
- `tests/fixtures/xbrl/simple/simple.xsd` — Synthetic extension taxonomy schema.
- `tests/integration/test_clickhouse_ddl.py` — Checks ClickHouse connectivity and idempotent DDL application.
- `tests/integration/test_postgres_control.py` — Checks PostgreSQL control repository connectivity.
- `tests/unit/test_arelle_helpers.py` — Tests deterministic hashing, period/arc classification, and Decimal coercion.
- `tests/unit/test_arrow_schemas.py` — Tests construction and strict mismatch rejection for the checked-in Arrow contracts when PyArrow is installed.
- `tests/unit/test_backfill_plan.py` — Tests stable semantic plan checksums and source-revision sensitivity.
- `tests/unit/test_bulk_catalog.py` — Tests bulk catalog parsing, period detection, and size conversion.
- `tests/unit/test_dimensions.py` — Tests order-independent dimension serialization and hashing.
- `tests/unit/test_filing_discovery.py` — Tests SEC submissions normalization into FilingRef.
- `tests/unit/test_object_store.py` — Tests immutable local object keys.
- `tests/unit/test_periods.py` — Tests instant/duration validation and quarter hints.
- `tests/unit/test_rate_limiter.py` — Tests request spacing.
- `tests/unit/test_schema_drift.py` — Tests exact, BOM, and reordered header handling.
- `tests/unit/test_sec_tsv.py` — Tests synthetic ZIP manifest and bounded batch reading when PyArrow is installed.

### Architecture and runbooks

- `docs/ARCHITECTURE.md` — End-to-end source, bronze, silver, gold, control-plane, idempotency, failure, and security design.
- `docs/ARELLE_IMPLEMENTATION.md` — Detailed Arelle loading, context, unit, fact, concept, relationship, issue, and test guide.
- `docs/BACKUP_RESTORE.md` — Backup scope, consistent procedure, rebuild path, and restore drills.
- `docs/CANONICAL_MAPPING.md` — Mapping precedence, prohibited shortcuts, evidence, and reproducibility policy.
- `docs/DATA_MODEL.md` — Keys and field-level intent for eight SEC tables, full XBRL, and canonical layers.
- `docs/DEPENDENCIES.md` — Purpose and upgrade policy for each dependency group.
- `docs/DEPLOYMENT.md` — Local, single-host production, scaling, environment, and release guidance.
- `docs/DEVELOPMENT.md` — Prerequisites, bootstrap, daily workflow, dependency policy, and stub completion rule.
- `docs/IMPLEMENTATION_ORDER.md` — Recommended phase order from foundation through operations.
- `docs/INGESTION_RUNBOOK.md` — Operator procedure for catalog, backfill, incremental filings, quarantine, and replay.
- `docs/OPERATIONS.md` — Routine checks, safe pause, incident handling, and production additions.
- `docs/REFERENCES.md` — Primary SEC, Arelle, EdgarTools, ClickHouse, and Parquet references.
- `docs/SCHEMA_EVOLUTION.md` — Rules for source, Arrow/Parquet, ClickHouse, PostgreSQL, and parser revisions.
- `docs/SEC_ACCESS.md` — SEC identity, rate, caching, retry, and source-access policy.
- `docs/TEST_PLAN.md` — Unit, integration, real-filing corpus, load, and reconciliation test matrix.
- `docs/decisions/ADR-001-storage-layers.md` — Decision record for PostgreSQL, ClickHouse, and object/Parquet separation.
- `docs/decisions/ADR-002-postgres-job-queue.md` — Decision record for starting with a SKIP LOCKED PostgreSQL queue.
- `docs/decisions/ADR-003-arelle-source-of-truth.md` — Decision record making Arelle the standards-aware parser boundary.

## Takeover sequence

1. Run `python scripts/verify_environment.py`, `python -m compileall src tests scripts`, and `pytest -m "not integration"`.
2. Start services, apply Alembic and ClickHouse DDL, then run integration tests.
3. Complete the historical FSNDS reader/coercion/Arrow insert path first.
4. Complete Arelle extraction against the synthetic fixture and a pinned real-filing corpus.
5. Connect daily discovery and package acquisition to the same raw model.
6. Add canonical mappings only after raw reconciliation is trustworthy.
7. Add scheduling, authentication, and production deployment hardening last.

The scaffold intentionally keeps implementation decisions visible. Search `IMPLEMENT_ME`; each remaining marker should be removed only together with tests and updated operational documentation.
