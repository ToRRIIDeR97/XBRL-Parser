# Dependency Map

## Core parsing and SEC access

- `arelle-release[EFM]`: authoritative XBRL/iXBRL loading, DTS traversal, validation, dimensions, relationships, and footnotes.
- `edgartools`: optional ergonomic SEC discovery and exploratory financial-statement access.
- `httpx`, `aiofiles`, `tenacity`: controlled SEC transport, streaming downloads, and retry primitives.
- SEC inline transformation registry (`ixt-sec`, namespace
  `http://www.sec.gov/inlineXBRL/transformation/2015-08-31`): an in-repo Arelle plugin,
  `infrastructure/xbrl/sec_transforms.py`, written for this project from the SEC
  "Inline XBRL Transformation Registry" semantics. No third-party code is vendored.
  It is versioned by `SEC_TRANSFORM_REGISTRY_VERSION` and covered by the repository
  license. `arelle-release` does not ship this registry.
- W3C `xml.xsd` (2001 and 2001/03 versions): unmodified copies in
  `infrastructure/xbrl/schemas/www.w3.org/`, taken from the copies bundled with
  `arelle-release` and redistributed under the W3C document license.
- Standard taxonomy packages (FASB us-gaap and srt, SEC dei and others): pinned by URL
  and SHA-256 in `config/taxonomy_packages.yml` and installed with
  `xbrl-warehouse taxonomy sync`. They are not committed.
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

## References

Accessed 2026-08-24.

- SEC Financial Statement and Notes Data Sets: https://www.sec.gov/data-research/sec-markets-data/financial-statement-notes-data-sets
- SEC Financial Statement Data Sets: https://www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets
- SEC FSNDS documentation: https://www.sec.gov/files/aqfsn_1.pdf
- SEC EDGAR APIs: https://www.sec.gov/search-filings/edgar-application-programming-interfaces
- SEC accessing EDGAR data: https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data
- Arelle repository: https://github.com/Arelle/Arelle
- Arelle documentation: https://arelle.readthedocs.io/
- Arelle Python package: https://pypi.org/project/arelle-release/
- EdgarTools repository: https://github.com/dgunning/edgartools
- ClickHouse Connect documentation: https://clickhouse.com/docs/integrations/language-clients/python
