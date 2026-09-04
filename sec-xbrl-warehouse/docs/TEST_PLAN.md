# Test Plan

## Unit tests

- configuration validation and SEC identity rejection;
- period and accession normalization;
- dimensional segment parsing;
- catalog HTML parsing;
- ZIP manifest and header drift detection;
- deterministic row/fact/context hashes;
- mapping precedence and explanation;
- job state transitions;
- ClickHouse SQL statement loading.

## Integration tests

- PostgreSQL migrations and concurrent job claims;
- MinIO immutable put/get/checksum behavior;
- ClickHouse Arrow inserts for each source table;
- end-to-end synthetic FSNDS ZIP load;
- Arelle synthetic XBRL extraction;
- API queries against loaded fixture data.

## Corpus tests

Maintain a manifest rather than embedding assumptions in code. The corpus should cover:

- standard US-GAAP annual filing;
- quarterly filing with YTD and quarterly durations;
- extension concepts;
- explicit and typed dimensions;
- text blocks and footnotes;
- multi-document inline XBRL;
- IFRS taxonomy;
- amendment;
- duplicate/inconsistent facts;
- calculation and presentation networks.

## Load tests

Generate large TSV members and assert:

- bounded resident memory;
- stable batch throughput;
- no Python row-by-row warehouse insertion;
- retry behavior under injected ClickHouse and object-store failures;
- restart from every stage checkpoint.

## Reconciliation tests

For sampled filings compare:

- Arelle fact counts vs SEC bulk rows where scopes overlap;
- concept/version/date/unit/dimension keys;
- numeric raw values and decimal attributes;
- presentation/calculation relationships;
- discrepancies recorded with source attribution.
## Coverage gates

The repository-wide branch-coverage floor is **70%** and is enforced in `pyproject.toml`. Do not lower it. Raise it to at least 75% before calling the first production backfill complete. Trust boundaries and newly changed ingestion modules should have direct failure/replay tests rather than relying only on the aggregate number.
