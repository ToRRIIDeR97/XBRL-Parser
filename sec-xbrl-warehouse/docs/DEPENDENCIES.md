# Dependency Map

## Core parsing and SEC access

- `arelle-release[EFM]`: authoritative XBRL/iXBRL loading, DTS traversal, validation, dimensions, relationships, and footnotes.
- `edgartools`: optional ergonomic SEC discovery and exploratory financial-statement access.
- `httpx`, `aiofiles`, `tenacity`: controlled SEC transport, streaming downloads, and retry primitives.
- `beautifulsoup4`, `lxml`: catalog markup and package/XML support. They must not replace Arelle's XBRL semantics.

## Data and storage

- `pyarrow`, `polars`: bounded batch transformations and Parquet. PyArrow is the database interchange contract.
- `clickhouse-connect[arrow]`: Arrow-native analytical warehouse client.
- `sqlalchemy[asyncio]`, `asyncpg`, `alembic`: PostgreSQL control plane and migrations.
- `boto3`: S3/MinIO immutable object storage.
- `orjson`: deterministic/high-throughput manifest and API serialization where appropriate.

## Service and operations

- `fastapi`, `uvicorn`: local query API.
- `typer`, `rich`: operator CLI.
- `structlog`, `prometheus-client`: structured logs and metrics.
- `pydantic`, `pydantic-settings`, `pyyaml`: typed contracts and configuration.

## Development

- `pytest`, `pytest-asyncio`, `pytest-cov`, `respx`: tests and HTTP simulation.
- `ruff`, `mypy`, `types-PyYAML`, `pre-commit`: style, type checks, and repository hygiene.

## Upgrade rule

Before upgrading Arelle, PyArrow, ClickHouse, SQLAlchemy, or the SEC convenience adapter, record the prior versions, run the synthetic suite and pinned real-filing corpus, compare structural counts/hashes, and create a new extraction/schema revision when output semantics change.
