# SEC XBRL Warehouse

A local-first, open-source fundamentals warehouse built from SEC XBRL data.

The project separates the system into four durable layers:

1. **Historical backfill:** SEC Financial Statement and Notes Data Sets (FSNDS), preserving the eight published tables: `SUB`, `TAG`, `DIM`, `NUM`, `TXT`, `REN`, `PRE`, and `CAL`.
2. **Incremental ingestion:** newly filed EDGAR documents discovered through SEC indexes/APIs and downloaded from the EDGAR archive.
3. **Authoritative parsing:** Arelle for XBRL/iXBRL semantics and validation; EdgarTools only as a convenience adapter for discovery and developer-facing filing access.
4. **Storage:** PostgreSQL for the control plane, ClickHouse for analytical facts, and S3-compatible object storage/MinIO for immutable raw files and Parquet.

The historical and incremental ingestion paths are implemented end to end. Production use still requires a real SEC identity, an acceptance corpus, a controlled backfill, and infrastructure-specific backup and security configuration.

## Included

- Docker Compose services for PostgreSQL, ClickHouse, and MinIO.
- PostgreSQL SQLAlchemy models and Alembic migrations.
- ClickHouse DDL for SEC bulk tables, full-fidelity XBRL facts, concepts, contexts, relationships, and canonical facts.
- Typed domain models for the SEC FSNDS tables and Arelle extraction results.
- SEC-aware HTTP client with an explicit user agent, throttling, retries, and resumable-download hooks.
- Bulk catalog acquisition, bounded ZIP/TSV streaming, deterministic Parquet, resumable workers, reconciliation, and replay-safe publication.
- Full-fidelity Arelle extraction, filing-package archiving, canonical mapping review, raw/canonical APIs, Prometheus metrics, and a Grafana dashboard.
- Current implementation and acceptance status in [`HANDOFF.md`](HANDOFF.md).
- A coding-agent operating contract in [`AGENTS.md`](AGENTS.md).
- System design and UML views in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md); dated build and audit records in [`docs/history/`](docs/history/README.md).

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

Enqueue the reviewed plan and run a worker:

```bash
xbrl-warehouse backfill enqueue data/plans/fsnds-2016-2026.json
xbrl-warehouse worker run
```

For recurring incremental discovery, schedule the idempotent rolling command:

```bash
xbrl-warehouse filing discover-latest --lookback-days 3
```

## Production acceptance sequence

1. Configure a real SEC identity and checksum-pinned taxonomy packages.
2. Run the documented real-filing corpus and compare it with the bulk source.
3. Restore a fresh copy of control state and immutable objects in an isolated environment.
4. Canary one recent bulk artifact and one incremental filing before the historical backfill.
5. Expand workers only after measuring SEC rate, Arelle memory, and ClickHouse throughput.

## Repository map

```text
sec-xbrl-warehouse/
├── AGENTS.md                 # Development and validation contract
├── HANDOFF.md                # Current operations and acceptance boundary
├── FUNCTION_STUBS.md         # Protocol/interface index
├── docker-compose.yml        # Storage, application, and monitoring profiles
├── ops/                      # Prometheus and Grafana provisioning
├── pyproject.toml            # Runtime/dev dependencies and tooling configuration
├── config/                   # Source, metric, mapping and validation configuration
├── docker/                   # Application image and database initialization assets
├── migrations/               # PostgreSQL Alembic migrations
├── sql/clickhouse/           # Analytical warehouse DDL and reference queries
├── src/sec_xbrl_warehouse/   # Application source code
├── scripts/                  # Bootstrap, diagnostics and helper scripts
├── tests/                    # Unit/integration contracts and synthetic fixtures
└── docs/                     # Architecture and UML views (diagrams/), runbooks, data model, history/
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
make monitoring
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

Implementation phases A–F are complete. See [`HANDOFF.md`](HANDOFF.md) for verified checks and the external production acceptance gates that remain; no live SEC corpus or production backfill is claimed.
