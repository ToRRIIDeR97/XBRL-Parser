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

The scaffold starts with a **35% repository-wide coverage floor** because large adapter and orchestration surfaces are intentionally stubbed. Do not lower this threshold. Raise it as milestones land: at least 60% after the historical loader, 70% after Arelle/incremental ingestion, and 75% or higher before calling the first production backfill complete. Newly implemented modules should target substantially higher direct coverage than the repository-wide floor.

