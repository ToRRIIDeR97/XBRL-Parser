# SEC XBRL Warehouse

A handoff-ready Python scaffold for building a local, open-source fundamentals warehouse from SEC XBRL data.

The project separates the system into four durable layers:

1. **Historical backfill:** SEC Financial Statement and Notes Data Sets (FSNDS), preserving the eight published tables: `SUB`, `TAG`, `DIM`, `NUM`, `TXT`, `REN`, `PRE`, and `CAL`.
2. **Incremental ingestion:** newly filed EDGAR documents discovered through SEC indexes/APIs and downloaded from the EDGAR archive.
3. **Authoritative parsing:** Arelle for XBRL/iXBRL semantics and validation; EdgarTools only as a convenience adapter for discovery and developer-facing filing access.
4. **Storage:** PostgreSQL for the control plane, ClickHouse for analytical facts, and S3-compatible object storage/MinIO for immutable raw files and Parquet.

This repository intentionally contains implementation stubs in the places where the next coding agent must make project-specific choices. Search for `IMPLEMENT_ME` to find every required implementation point. Each stub has input/output contracts, invariants, failure behavior, and acceptance criteria in its docstring.

## What is already included

- Docker Compose services for PostgreSQL, ClickHouse, and MinIO.
- PostgreSQL SQLAlchemy models and an initial Alembic migration.
- ClickHouse DDL for SEC bulk tables, full-fidelity XBRL facts, concepts, contexts, relationships, and canonical facts.
- Typed domain models for the SEC FSNDS tables and Arelle extraction results.
- SEC-aware HTTP client with an explicit user agent, throttling, retries, and resumable-download hooks.
- Bulk catalog parsing, ZIP/TSV streaming interfaces, Parquet/object-store abstractions, job contracts, CLI commands, API routes, tests, and synthetic fixtures.
- A file-by-file implementation handoff in [`HANDOFF.md`](HANDOFF.md).
- A generated symbol-level stub and interface index in [`FUNCTION_STUBS.md`](FUNCTION_STUBS.md).
- A coding-agent operating contract in [`AGENTS.md`](AGENTS.md).
- A transparent build/validation record in [`BUILD_REPORT.md`](BUILD_REPORT.md).
- A per-file integrity list in `MANIFEST.sha256`.

## Quick start

### 1. Configure the environment

```bash
cp .env.example .env
```

Edit `.env` and replace the SEC identity placeholder. The SEC expects an identifying user agent containing a name or organization and a monitored email address.

### 2. Start infrastructure

```bash
docker compose up -d postgres clickhouse minio minio-init
```

### 3. Install the Python project

Python 3.12 is the recommended runtime.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 4. Initialize schemas

```bash
alembic upgrade head
xbrl-warehouse db init-clickhouse
xbrl-warehouse doctor
```

### 5. Inspect and plan the historical backfill

```bash
xbrl-warehouse catalog sync --dataset notes
xbrl-warehouse backfill plan --dataset notes --from-year 2016 --to-year 2026 --output data/plans/fsnds-2016-2026.json
```

`backfill run`, Arelle extraction, and production warehouse writes remain explicit stubs. Their implementation order is documented in `HANDOFF.md`.

## Recommended implementation sequence

1. Make the bulk FSNDS loader complete and idempotent.
2. Implement ClickHouse Arrow inserts and PostgreSQL artifact/job state transitions.
3. Implement Arelle extraction against the synthetic fixture, then a small SEC filing corpus.
4. Add incremental EDGAR discovery and filing-package acquisition.
5. Build canonical concept mapping only after the raw/as-filed layer is verified.
6. Enable the API and scheduled workers after replay and recovery tests pass.

## Repository map

```text
sec-xbrl-warehouse/
├── AGENTS.md                 # Instructions for the coding agent taking over
├── BUILD_REPORT.md           # Delivered scope, validation, and known limitations
├── HANDOFF.md                # File-by-file implementation contract
├── FUNCTION_STUBS.md         # Exact implementation symbols and signatures
├── MANIFEST.sha256           # Integrity digest for every packaged repository file
├── PROJECT_STRUCTURE.md      # Complete inventory of files and responsibilities
├── PROJECT_TREE.txt          # Generated full repository tree
├── docker-compose.yml        # PostgreSQL, ClickHouse, MinIO, API and worker profiles
├── pyproject.toml            # Runtime/dev dependencies and tooling configuration
├── config/                   # Source, metric, mapping and validation configuration
├── docker/                   # Application image and database initialization assets
├── migrations/               # PostgreSQL Alembic migrations
├── sql/clickhouse/           # Analytical warehouse DDL and reference queries
├── src/sec_xbrl_warehouse/   # Application source code
├── scripts/                  # Bootstrap, diagnostics and helper scripts
├── tests/                    # Unit/integration contracts and synthetic fixtures
└── docs/                     # Architecture, runbooks, data model and implementation notes
```

## Non-negotiable data guarantees

- Raw source artifacts are immutable and checksum-addressed.
- The original accession, concept namespace/name, raw value, context, unit, dimensions, decimals, precision, and source document are never discarded.
- Canonical metrics are written to a separate layer and remain traceable to the raw fact and mapping decision.
- Every ingestion operation is replayable and idempotent.
- Schema drift is quarantined rather than silently coerced.
- SEC source access stays below the configured request ceiling and always uses the declared identity.

## Useful commands

```bash
make help
make up
make install
make migrate
make init-clickhouse
make doctor
make test
make lint
make typecheck
make scan-stubs
```

## Status

This ZIP is a **complete engineering scaffold, not a finished extractor**. It is designed so another agent can continue without having to rediscover the architecture, table contracts, infrastructure, or implementation boundaries. See `BUILD_REPORT.md` for the exact checks run and the runtime checks that remain for CI or a Docker-enabled machine.
