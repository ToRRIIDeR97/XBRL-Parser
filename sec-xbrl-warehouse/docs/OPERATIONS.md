# Operations Runbook

## Service order

1. PostgreSQL
2. ClickHouse
3. MinIO/object storage
4. Alembic migration
5. ClickHouse DDL
6. worker
7. API

## Routine checks

- `/health/ready` is green.
- Job queue has no unexplained `dead` growth.
- SEC 429/5xx and retry counts remain low.
- Bronze object count and PostgreSQL artifact count reconcile.
- ClickHouse disk free space exceeds the configured safety threshold.
- Quarantine and mapping-review queues are triaged.

## Safe pause

Stop producers first, then wait for active jobs to checkpoint. Stop workers only after heartbeats cease. PostgreSQL and object storage are the source of recovery state; ClickHouse can be rebuilt from silver Parquet.

## Incident categories

- Source/access: reduce concurrency/rate, inspect identity and SEC status, preserve retries.
- Schema drift: quarantine only the changed artifact, compare SEC documentation and captured header.
- Parser regression: pin Arelle version, retain package/log, rerun corpus.
- Partial warehouse write: replay the same artifact/accession revision and reconcile latest-state counts.
- Mapping error: create a new mapping version; never mutate historical mapping lineage in place.

## Production additions

Add authentication, TLS, secret management, network policy, backup monitoring, object lifecycle rules, alert routing, and a scheduler only after the local replay path is proven.
