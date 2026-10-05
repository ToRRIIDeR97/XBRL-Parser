# Build and Validation Report

**Implementation completion date:** 2026-09-04

**Runtime tested:** Python 3.12.4

**Database integration:** PostgreSQL 17 and ClickHouse 25.8

## Delivered scope

- Immutable, checksum-addressed SEC bulk and filing-package acquisition with resumable HTTP validation.
- Bounded eight-table FSNDS parsing, deterministic Parquet, ClickHouse publication, checkpoints, quarantine, and reconciliation.
- Full-fidelity Arelle extraction for contexts, units, concepts, facts, dimensions, networks, labels/references, footnotes, validation, and lineage.
- Daily and company filing discovery, restartable incremental ingestion, and bulk/incremental comparison.
- Fenced PostgreSQL workers, stale-lock recovery, versioned artifacts/mappings, and canonical review/approval.
- Replay-safe raw/latest/canonical ClickHouse views, bounded FastAPI queries, and derived-metric lineage.
- Prometheus instrumentation for API and worker processes, a provisioned Grafana dashboard, scheduler command, preserving schema upgrade, and operational runbooks.

## Validation

The final commands and exact results are recorded here after the implementation run:

| Check | Result |
|---|---|
| Non-integration tests | 117 passed, 14 deselected |
| Repository branch coverage | 72.30% (70% enforced) |
| PostgreSQL/ClickHouse/MinIO integration tests | 14 passed using disposable databases/bucket |
| PostgreSQL backup/restore smoke drill | Passed in two disposable databases; both removed |
| Ruff lint and format | Passed |
| Strict mypy | Passed |
| Python bytecode compilation | Passed |
| Stub scan | 0 implementation markers |
| Compose/YAML/JSON validation | Passed |

Database tests create uniquely named databases, migrate them, and remove them after each run. No application database was truncated and no production schema upgrade was executed.

## Not claimed

- No real SEC download was made without an operator-provided identifying user agent.
- The real-filing corpus, 2016-to-current backfill, 100-filing independent reconciliation sample, and target-environment restore drill remain production acceptance work.
- The local Compose monitoring profile is not a substitute for production TLS, API authentication, managed secrets, replicated objects, pinned image digests, or alert routing.

See `../../HANDOFF.md` and `IMPLEMENTATION_STATUS_2026-09-04.md` for the operating sequence and status boundary.
