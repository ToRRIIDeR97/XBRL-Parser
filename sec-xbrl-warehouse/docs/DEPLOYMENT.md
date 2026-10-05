> September 8 remediation: before adopting the changed Compose configuration, read
> [the deployment prerequisites](HARDENING_REMEDIATION_2026-09-08.md#deployment-prerequisites).
> The API no longer receives writer credentials or the shared data volume. Use an
> operator/worker image with an explicit CLI command for privileged bootstrap.
> Current ingestion is paused; do not deploy or resume it from these examples.

# Deployment Guide

## Local development

Use `docker-compose.yml` for PostgreSQL, ClickHouse, and MinIO. Run API and worker locally or enable the `application` Compose profile.

```bash
docker compose --profile application up -d
```

Add the provisioned Prometheus/Grafana stack with both profiles:

```bash
docker compose --profile application --profile monitoring up -d
```

The local API, Prometheus, Grafana, database, and object-store host ports bind to loopback where configured. Do not treat local Compose credentials or anonymous local Grafana access as production security.

The application image runs as UID/GID `10001:10001`, with Linux capabilities
dropped and privilege escalation disabled by Compose. Fresh `app_data` volumes
inherit that ownership. Before upgrading an existing root-owned application volume,
stop API/workers and have the operator set ownership of that volume to
`10001:10001`; do not change ownership of the PostgreSQL, ClickHouse, or MinIO
volumes. Bind-mounted application data and parser caches must also be writable by
that UID. Bucket initialization must succeed before API/workers start.

Readiness remains unauthenticated for infrastructure probes but allows only one
database probe at a time per API process; overlapping probes return HTTP 503.
Liveness does no database work and stays available during query saturation.
When `XBRL_API_KEY` is configured, Prometheus needs matching bearer credentials
for `/metrics/`; supply them through a protected credentials file and Prometheus's
`authorization.credentials_file` setting. The bundled monitoring config is for
local unauthenticated development.

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
- separate SEC egress rate limiting across all worker replicas;
- pinned taxonomy packages installed with `taxonomy sync`, and `XBRL_ARELLE_OFFLINE=true`.

Before starting workers on a production host, run `xbrl-warehouse taxonomy sync`
through the `worker` service, then set `XBRL_ARELLE_OFFLINE=true`. `doctor` fails in
production while offline mode is off or any enabled taxonomy package is missing.
Keep `XBRL_ARELLE_TAXONOMY_PACKAGES_DIR` on persistent storage writable by UID 10001.

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

## Scheduling

Run `xbrl-warehouse filing discover-latest --lookback-days 3` daily with cron, systemd, Kubernetes CronJob, or the platform scheduler. The command only enqueues idempotent work. Keep workers as supervised services and coordinate the aggregate SEC request rate before adding replicas.
