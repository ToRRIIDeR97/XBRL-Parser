# ADR-002: PostgreSQL Job Queue First

## Status

Accepted.

## Decision

Use an `ingestion_jobs` table and `FOR UPDATE SKIP LOCKED` before adding Redis, Kafka, or a workflow platform.

## Consequences

- Simple local deployment and transactional idempotency.
- Adequate for initial batch concurrency.
- Requires explicit heartbeats, stale-lock recovery, and bounded job payloads.
- Revisit only after measured queue contention or orchestration requirements justify another system.
