# 07 — Companies table from SEC submissions data

Status: plan only. Code follows after this plan is reviewed.

## Problem

The warehouse has no company dimension. `filings` stores only `cik` and the
`company_name` captured at discovery. The SEC submissions JSON, which discovery
already downloads per CIK ([filing_discovery.py:53](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/ingestion/filing_discovery.py)), carries tickers, exchanges,
SIC code, fiscal year end, state of incorporation, filer category and former names.
All of that is discarded after the filing list is parsed.

What this blocks:

- Ticker-to-CIK resolution (`AAPL` → 320193), which the research pipeline and any
  operator command need.
- Fiscal-year-end checks for plan 11's quarter logic.
- SIC/industry grouping, for example routing banks to the bank metric pack.
- Name history for old filings.

### Before / after

| | Before | After |
|---|---|---|
| Ticker lookup | none | `find_company_by_ticker("aapl")` → CIK 320193 |
| Fiscal year end | none | `"0927"` from `fiscalYearEnd` |
| Former names | dropped | kept with validity dates |
| Daily-index discovery (no submissions read) | no company data | one submissions request per *new* CIK, then kept up to date by refreshes |

## Scope

Likely files:

- `domain/models.py`:
  - `Company` with `cik`, `name`, `entity_type`, `sic`, `sic_description`,
    `tickers: tuple[str, ...]`, `exchanges: tuple[str, ...]`, `fiscal_year_end`
    (`MMDD` or `None`), `state_of_incorporation`, `filer_category`,
    `former_names: tuple[FormerName, ...]`, `source_checksum`, `fetched_at`.
  - `FormerName` with `name`, `valid_from`, `valid_to`.
- `ingestion/filing_discovery.py`:
  - `parse_company(payload) -> Company`.
  - `SecSubmissionsDiscovery.discover_with_company(cik, *, forms, ...)` returning
    `(Company, filings)` from the same single request. `discover` keeps its contract.
  - `enrich_companies(control, http, *, ciks, as_of) -> int`, which fetches
    submissions only for CIKs without a company row.
- `domain/ports.py` (`ControlRepository`), `infrastructure/storage/postgres.py`,
  `postgres_models.py`, plus an Alembic migration:
  - `companies` table (`cik` PK, scalar columns, `tickers text[]`,
    `exchanges text[]`, `former_names jsonb`, timestamps);
  - `upsert_company`, `get_company`, `find_company_by_ticker`, `known_company_ciks`.
- `worker.py`: after a `discover_filings` job, call `enrich_companies` for the
  discovered CIKs.
- `cli.py`: `filing discover` stores the company. A new `company show <cik|ticker>`
  command.
- `docs/DATA_MODEL.md`, `docs/INGESTION_RUNBOOK.md`.

Exclusions:

- Exposing companies through the read-only API and ClickHouse is plan 11 (the API
  has no PostgreSQL reader today).
- EIN, phone and addresses aren't stored. They aren't needed, and keeping them out
  keeps the table free of contact data.

Invariants:

- SEC access stays within the rate limiter and the single-SEC-process rule.
  Enrichment is bounded by the number of new CIKs per discovery run.
- A ticker belongs to at most one company at a time. A newer upsert that lists a
  ticker removes it from the previous holder.

## Acceptance criteria

- **AC-1 Parsing.** `parse_company` maps the submissions fields (fixture
  `submissions_company.json`, modeled on AAPL):
  - `cik` as an int (string with leading zeros accepted);
  - `tickers` and `exchanges` as tuples in source order;
  - `sic` as an int;
  - `fiscal_year_end` as a valid `MMDD` or `None`; malformed values such as `1332`
    become `None`;
  - former names with `valid_from`/`valid_to` dates.

  Missing optional fields give `None` or empty tuples, never errors.
- **AC-2 Persistence.** `upsert_company` and `get_company` round-trip every field.
  `find_company_by_ticker` is case-insensitive and returns `None` when the ticker is
  unknown. Re-upserting the same CIK updates in place.
- **AC-3 Single request.** `discover_with_company` returns the parsed company and the
  same filings `discover` would return, using one submissions request.
- **AC-4 Ticker moves.** When company B is upserted with a ticker that company A
  held, lookups return B, and A no longer lists the ticker.
- **AC-5 Enrichment.** `enrich_companies` requests submissions only for CIKs that
  `known_company_ciks` doesn't return. It dedupes the input, upserts each result,
  and returns the number of companies stored.

## Implementation sequence

1. Domain models and `parse_company` (AC-1).
2. Migration and repository methods (AC-2, AC-4).
3. `discover_with_company`, `enrich_companies`, worker and CLI wiring (AC-3, AC-5).
4. Docs. Validate suite including integration.
5. Operator step: `filing discover --cik 320193` (or the next discovery run).
   Confirm `company show AAPL`.

## Verification

From `sec-xbrl-warehouse/`, with the local storage services running for the
integration tests:

```bash
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/07-companies-table/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/07-companies-table/verification
```

### Baseline (main @ 8f16ca0, 5 October 2026)

All 10 tests fail because the interfaces are new: `parse_company`,
`discover_with_company`, `enrich_companies`, and the repository methods.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
