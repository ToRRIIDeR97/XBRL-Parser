# ADR-001: Bronze/Silver/Gold Storage Layers

## Status

Accepted.

## Decision

Use immutable object storage for source artifacts, Parquet for typed rebuildable datasets, and ClickHouse for serving analytical queries. PostgreSQL stores the control plane and mapping decisions.

## Consequences

- More components than a single relational database.
- Clear replay and audit trail.
- ClickHouse can be rebuilt without re-downloading SEC data.
- Canonical transformations cannot destroy source fidelity.
