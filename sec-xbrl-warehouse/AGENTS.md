# Coding Agent Contract

Read this file, `HANDOFF.md`, `FUNCTION_STUBS.md`, `docs/ARCHITECTURE.md`, and `docs/DATA_MODEL.md` before changing code.

## Mission

Complete a reliable SEC XBRL warehouse without replacing standards-aware XBRL processing with ad-hoc HTML/XML scraping.

## Hard constraints

1. **Use Arelle for XBRL/iXBRL semantics.** EdgarTools may assist with discovery, filing access, or standardized convenience views, but it is not the source of truth for full-fidelity extraction.
2. **Preserve as-filed data.** Never overwrite raw concepts, values, units, contexts, dimensions, labels, or relationships with canonical values.
3. **Keep normalization reversible.** Every canonical fact must point to the raw fact and mapping rule that produced it.
4. **Be idempotent.** Re-running any artifact or accession must not create duplicate logical rows.
5. **Quarantine drift.** Unknown columns, malformed ZIPs, unparseable facts, and unsupported contexts must be retained with diagnostics; do not silently drop them.
6. **Respect SEC access rules.** Send the configured user agent, stay at or below the configured requests-per-second limit, retry with jitter, and cache/reuse source artifacts.
7. **Do not delete raw artifacts after successful loading.** Retention policy is a separate, explicit operational decision.
8. **Do not introduce a distributed queue until the PostgreSQL job queue has been proven insufficient.** The supplied `FOR UPDATE SKIP LOCKED` design is the initial orchestration mechanism.
9. **Use Arrow/Parquet batches.** Do not materialize full historical files as Python lists or pandas DataFrames.
10. **Keep platform support.** Development must remain workable on macOS, Linux, and Windows through Docker Desktop and Python 3.12.

## How to work

- Search for `IMPLEMENT_ME` and complete stubs in the order listed in `HANDOFF.md`.
- Add or update tests before removing an `IMPLEMENT_ME` marker.
- Keep external-library calls behind adapters so Arelle, EdgarTools, ClickHouse, or object storage can be upgraded independently.
- Prefer explicit typed records and Arrow schemas over free-form dictionaries at ingestion boundaries.
- Log accession, artifact ID, table, batch number, row count, byte count, and elapsed time on every stage transition.
- Record source checksums and software versions for reproducibility.

## Required validation before declaring a milestone complete

```bash
python -m compileall src tests scripts
ruff check .
ruff format --check .
mypy src
pytest -m "not integration"
```

For database milestones also run:

```bash
docker compose up -d postgres clickhouse minio minio-init
alembic upgrade head
xbrl-warehouse db init-clickhouse
pytest -m integration
```

## Completion standard

A feature is not complete merely because it parses one filing. It must demonstrate:

- deterministic replay;
- duplicate handling;
- error recovery;
- schema-drift behavior;
- bounded memory usage;
- database row reconciliation;
- raw-to-canonical lineage;
- tests covering at least one standard concept, one extension concept, one dimensional fact, one text block, one duplicate fact, and one amended filing.
