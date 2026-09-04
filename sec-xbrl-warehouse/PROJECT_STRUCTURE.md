# Project Structure

## Runtime topology

| Concern | Component | Source of truth |
|---|---|---|
| Historical source | SEC Financial Statement and Notes ZIPs | Immutable bronze object |
| Incremental source | EDGAR accession package | Immutable package and manifest |
| XBRL semantics | Arelle adapter | Versioned raw extraction bundle |
| Control state | PostgreSQL | Artifacts, filings, jobs, checkpoints, issues, mappings |
| Analytical facts | ClickHouse | Rebuildable from object storage and control state |
| Durable files | S3/MinIO or local object store | Source bytes, diagnostics, Parquet |
| Query surface | FastAPI and CLI | Bounded parameterized services |
| Monitoring | Prometheus and Grafana | API/worker process metrics |

## Ownership map

- `config/` — SEC sources, validation policy, taxonomy policy, canonical metrics, and mapping seeds.
- `migrations/` — Alembic-managed PostgreSQL control-plane schema.
- `sql/clickhouse/` — Versioned warehouse tables, publication markers, latest-state views, and reference queries.
- `src/sec_xbrl_warehouse/domain/` — Immutable records and adapter protocols.
- `src/sec_xbrl_warehouse/infrastructure/http/` — SEC-only throttled, resumable transport.
- `src/sec_xbrl_warehouse/infrastructure/storage/` — PostgreSQL, ClickHouse, Arrow, Parquet, hashing, and immutable object storage.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/` — Arelle boundary and optional EdgarTools convenience adapter.
- `src/sec_xbrl_warehouse/ingestion/` — Bulk catalog/download/load and filing discovery/package/incremental orchestration.
- `src/sec_xbrl_warehouse/mapping/` — Approved canonical mapping and review repository.
- `src/sec_xbrl_warehouse/query/` and `api/` — Bounded raw/canonical queries and HTTP endpoints.
- `src/sec_xbrl_warehouse/validation/` — Schema, source/silver/gold, XBRL, and filing reconciliation.
- `src/sec_xbrl_warehouse/worker.py` — Job services, handlers, fencing, heartbeats, retry, and graceful shutdown.
- `ops/` — Prometheus scraping and provisioned Grafana dashboard.
- `tests/unit/` — Determinism, trust-boundary, recovery, and command tests.
- `tests/integration/` — Disposable PostgreSQL/ClickHouse migration, fencing, insert, replay, and mapping tests.
- `docs/` — Architecture, SEC access, deployment, recovery, schema, testing, and operations runbooks.

`PROJECT_TREE.txt` is generated from the live repository with `python scripts/generate_project_tree.py`.

## Change order

1. Trace the source byte or job payload through its shared adapter and checkpoint boundary.
2. Make the smallest change at that shared boundary.
3. Add one focused failure/replay regression.
4. Run static checks and the non-integration suite; run disposable database tests for schema/repository changes.
5. Update the affected runbook and `HANDOFF.md` acceptance boundary.

Never point tests at an application database, overwrite immutable object keys, or collapse raw facts into canonical interpretations.
