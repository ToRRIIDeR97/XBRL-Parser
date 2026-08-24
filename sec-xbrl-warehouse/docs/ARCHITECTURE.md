# Architecture

## Goal

Build a replayable warehouse that captures both the SEC's historical flattened XBRL data and the full semantics of newly filed XBRL/iXBRL documents.

## System boundaries

```text
                         SEC public data
           ┌──────────────────┴──────────────────┐
           │                                     │
   FSNDS bulk ZIPs                       EDGAR indexes/APIs
   SUB/TAG/DIM/...                       filing packages
           │                                     │
           ▼                                     ▼
   Bulk catalog/download                 Filing discovery/download
           │                                     │
           └──────────────┬──────────────────────┘
                          ▼
                  Immutable bronze storage
                  ZIPs, package files, manifests
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
      Streaming TSV parser       Arelle XBRL adapter
             │                         │
             └────────────┬────────────┘
                          ▼
                    Typed domain records
                          │
             ┌────────────┴────────────┐
             │                         │
             ▼                         ▼
     Silver Parquet archive      ClickHouse raw warehouse
             │                         │
             └────────────┬────────────┘
                          ▼
               Mapping and normalization layer
                          │
                          ▼
                Canonical ClickHouse facts
                          │
                          ▼
                     Query service/API

PostgreSQL is the control plane throughout: artifacts, filings, jobs, runs,
validation issues, mapping versions, review state, and lineage metadata.
```

## Why two ingestion paths

The SEC bulk data is the fastest and most reliable historical backfill, but it is flattened and does not preserve every piece of original filing metadata. Arelle processing is therefore used for new filings, repair jobs, validation, and sampled historical comparison. The two sources remain separately identifiable and are reconciled rather than silently merged.

## Storage layers

### Bronze: immutable source artifacts

- SEC bulk ZIPs.
- EDGAR filing package files or a lossless package ZIP.
- HTTP metadata, checksums, source URLs, and acquisition timestamps.
- A manifest containing every member and checksum.

Bronze object keys include a source revision or checksum. Existing objects are never overwritten with different bytes.

### Silver: typed Parquet

- One dataset per SEC table or Arelle entity type.
- Partitioned by source period, filed year, table, and/or accession.
- ZSTD compression.
- Includes source artifact ID, ingest timestamp, schema version, and row hash.

Silver is the rebuild source for ClickHouse and protects against database corruption or schema experiments.

### Gold: ClickHouse

- Raw SEC bulk tables.
- Full-fidelity XBRL facts, contexts, concepts, and relationships.
- Canonical facts with explicit mapping lineage.
- Query-optimized denormalization such as CIK, filing date, form, and period fields on facts.

### PostgreSQL control plane

- Company and filing metadata.
- Source artifacts and object locations.
- Ingestion jobs and attempts.
- Validation and reconciliation results.
- Canonical metric definitions and concept mappings.
- Schema and software version history.

## Idempotency

### Bulk artifacts

Logical identity is `(dataset_kind, source_period, source_url, checksum)`. If the SEC republishes a period with changed bytes, it becomes a new revision and is loaded with a higher `_source_revision`.

### Filings

Logical identity is the dashed accession number. Package bytes and extraction version are tracked independently, allowing a parser upgrade to produce a new extraction revision without changing the filing identity.

### ClickHouse

Tables use `ReplacingMergeTree` with explicit revision/version columns. Do not depend on background merges for correctness in user-facing queries; use `argMax`, `FINAL` only where justified, or stable latest-state views.

## Job orchestration

The first implementation uses PostgreSQL as a queue:

1. A producer inserts a job with a unique idempotency key.
2. Workers claim rows inside a transaction using `FOR UPDATE SKIP LOCKED`.
3. A heartbeat updates the lock timestamp.
4. A stale lock becomes claimable after the configured timeout.
5. Each stage writes a checkpoint so the next attempt resumes rather than restarts.

This keeps the local deployment small and avoids Redis/Kafka until throughput evidence supports adding them.

## Failure domains

- **Network failure:** retry and resume download; never mark acquired until checksum is complete.
- **Malformed ZIP/package:** retain in quarantine with manifest and exception.
- **Schema drift:** stop before writes; record old/new headers.
- **Parser failure:** retain package, Arelle logs, software versions, and traceback.
- **Partial Parquet/ClickHouse write:** stage under a run/revision ID, reconcile, then promote state.
- **Mapping ambiguity:** leave raw fact unmapped and create a review candidate.

## Security

- No SEC API key is required for public data, but every request uses the configured identifying user agent.
- Credentials are supplied through environment variables and never committed.
- Object storage is private by default.
- Query API is intentionally unauthenticated only in the local scaffold; add authentication before exposing it beyond localhost.
