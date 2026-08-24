# Deployment Guide

## Local development

Use `docker-compose.yml` for PostgreSQL, ClickHouse, and MinIO. Run API and worker locally or enable the `application` Compose profile.

```bash
docker compose --profile application up -d
```

## Single-host production starting point

A small deployment can retain the same service topology on one protected host, but must add:

- pinned container digests;
- TLS/reverse proxy and API authentication;
- secrets outside `.env` and source control;
- private database/object-storage networks;
- persistent volumes on monitored disks;
- object replication and PostgreSQL backups;
- CPU/memory limits, log rotation, and alerts;
- a scheduler that only enqueues idempotent jobs;
- separate SEC egress rate limiting across all worker replicas.

## Scaling boundaries

Scale workers only after SEC request rate is globally coordinated. ClickHouse can scale vertically first. PostgreSQL queue contention, object-store throughput, and Arelle CPU/memory measurements should drive any move to Redis, Kafka, or distributed orchestration—not architectural fashion.

## Environment separation

Use independent PostgreSQL databases, ClickHouse databases, buckets, credentials, and job queues for development, test, and production. Never point a developer worker at production control state.

## Release procedure

1. Build an immutable image tagged with Git commit and dependency lock.
2. Run unit, integration, synthetic XBRL, and corpus comparisons.
3. Back up PostgreSQL and confirm object replication.
4. Apply backward-compatible control-plane and warehouse migrations.
5. Deploy API, then one canary worker.
6. Verify metrics/reconciliation and expand worker count.
7. Retain the prior image and parser revision for rollback/replay.
