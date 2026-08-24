# Development Setup

## Prerequisites

- Python 3.12 recommended.
- Docker Engine with Compose v2.
- At least 8 GB RAM for comfortable local development.
- Sufficient disk for bronze ZIPs, silver Parquet, database volumes, and temporary extraction.

## Bootstrap

macOS/Linux:

```bash
./scripts/bootstrap.sh
```

Windows PowerShell:

```powershell
.\scripts\bootstrap.ps1
```

The bootstrap intentionally runs `doctor --skip-sec-identity`. Before any SEC request, replace the template identity in `.env` and run the full doctor command.

For reproducible Arelle runs, create or retain the directories configured by `XBRL_ARELLE_CACHE_DIR` and `XBRL_ARELLE_TAXONOMY_PACKAGES_DIR`. Review `config/taxonomy_packages.yml` before enabling offline production parsing.

## Daily loop

```bash
source .venv/bin/activate
make up
make migrate
make init-clickhouse
make test
make lint
make typecheck
make scan-stubs
```

## Dependency policy

- Keep Arelle behind the infrastructure adapter.
- Keep SEC transport behind `SecHttpClient`.
- Keep Arrow schemas checked in and reviewed like database migrations.
- Upgrade one storage/parser dependency at a time and re-run the filing corpus.
- Pin container digests before production deployment.

## Definition of a completed stub

A stub is complete only when its marker is removed, its contract tests pass, failure/retry behavior is covered, and the relevant runbook is updated. Do not remove `IMPLEMENT_ME` merely because a happy-path implementation exists.
