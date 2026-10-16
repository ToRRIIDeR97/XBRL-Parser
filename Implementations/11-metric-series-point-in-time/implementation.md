# 11 — Standardized metric series with point-in-time views

Status: implemented and merged to main. Depends on plan 02
(fiscal fields) and plan 06 (metrics, approved mappings, `priority` on seeds).

## Problem

`canonical_facts` (once plan 06 fills it) holds one row per raw fact. That isn't a
usable time series:

- **Repeats.** The same period appears in many filings (comparative columns),
  sometimes with restated values.
- **Year-to-date cash flows.** 10-Q cash flows are year-to-date. Q2 and Q3 have to
  be derived (H1 − Q1, 9M − H1), and Q4 = FY − 9M, because the 10-K reports only the
  full year.
- **No TTM.** Nothing computes trailing twelve months.
- **No acceptance time.** `xbrl_facts` and `canonical_facts` carry only `filed_date`.
  The SEC acceptance timestamp is in PostgreSQL (`filings.accepted_at`) but not in
  ClickHouse. A backtest needs "what was known at time T", which requires acceptance
  times on every value.
- **Competing concepts.** When a filing reports both `Revenues` and
  `RevenueFromContractWithCustomerExcludingAssessedTax`, nothing chooses between them.

The research pipeline needs 20 quarters plus FY and TTM, as-reported and latest, and
values as of a cutoff date. This plan is the core of that integration.

### Before / after (Apple-like FY2025 operating cash flow, year-to-date in 10-Qs)

| Point | Before | After |
|---|---|---|
| Q1 | 1243 (raw) | 1243, `reported` |
| Q2 | only H1 = 2197 | 954, `ytd_difference` |
| Q3 | only 9M = 3137 | 940, `ytd_difference` |
| Q4 | not available | 1025, `annual_minus_nine_months` |
| TTM at FY end | not available | 4162, `ttm_sum` |
| FY2024 revenue as of 2025-01-01, after a later restatement | the latest value only | the value known on that date (3910) |

## Scope

Likely files:

- `domain/canonical.py`: `CanonicalFact.accepted_at: datetime | None`.
- `mapping/canonical_mapper.py`: copy `filing.accepted_at` into canonical facts.
- `infrastructure/storage/arrow_schemas.py`, `clickhouse.py`, `sql/clickhouse/`:
  `accepted_at DateTime64(3,'UTC')` on `xbrl_facts` and `canonical_facts`. Facts rows
  get it from `bundle.filing.accepted_at`.
- New `normalization/series.py`, pure functions:
  - `MetricObservation`: `cik, metric, concept_qname, accession, form, accepted_at,
    fiscal_year, fiscal_period, filing_period_end, period_start, period_end,
    period_instant, value, unit, priority=1, dimensions_json="[]"`;
  - `SeriesPoint`: `cik, metric, period_type ∈ {quarter, annual, ttm, instant},
    fiscal_year, fiscal_quarter, period_start, period_end, value, unit, derivation ∈
    {reported, ytd_difference, annual_minus_nine_months, ttm_sum, formula},
    concept_qname, source_accessions, available_at`;
  - `build_series(observations, *, view="latest"|"as_reported", as_of=None)`.
- New `query/series.py`: `rebuild_metric_series(warehouse, cik)` reads
  `v_canonical_facts_latest` (plus filing fiscal fields) and writes the
  `metric_series` ClickHouse table (both views, keyed by `available_at` so as-of
  queries can filter).
- `query/service.py`: `QueryService.metric_series(*, cik, metrics, view="latest",
  as_of=None)` with the existing query budgets.
- `api/routers/`: `GET /v1/series/{cik}?metrics=…&view=…&as_of=…`. Optional
  `ticker=` resolution through a ClickHouse `companies` mirror (from plan 07's table).
- Worker: rebuild a company's series after `map_canonical` or `ingest_filing` publish.
- `docs/DATA_MODEL.md`, `docs/CANONICAL_MAPPING.md`, `docs/ARCHITECTURE.md`, plus an
  API docs example.

### Derivation rules

- Duration lengths: quarter 80–100 days; six months 170–190; nine months 260–285;
  year 350–380. This covers 52/53-week calendars.
- Discrete quarters:
  - reported 3-month values;
  - otherwise the difference of consecutive year-to-date values with the same
    `period_start`;
  - Q4 = year − nine months, same `period_start`.
- A derived point needs all its inputs. Nothing is fabricated or calendar-guessed.
- TTM: the sum of four contiguous discrete quarters (each quarter starts the day after
  the previous one ends). TTM is never computed for instants.
- Fiscal labels: a point takes `fiscal_year`/`fiscal_period` from the filing whose
  `filing_period_end` equals the point's `period_end`. FY maps to quarter 4 for
  quarter points.
- Selection per `(metric, period)`:
  - only `dimensions_json == "[]"`;
  - within one filing, the lowest `priority` wins;
  - across filings, `latest` uses the newest `accepted_at` and `as_reported` the
    oldest;
  - `as_of` drops observations accepted after the cutoff *before* any derivation.
- `available_at` = the newest `accepted_at` among a point's inputs. `source_accessions`
  lists every input filing.
- Formula metrics (`free_cash_flow = operating_cash_flow - capital_expenditures`) are
  computed per period only when every operand exists, with `derivation="formula"`.

Exclusions:

- Segment series (plan 12).
- Currency conversion; every series keeps its reported unit.

Invariants:

- Lineage: every point can be traced to accessions and concepts. Canonical facts
  still point to raw facts and mapping versions.
- The series is fully rebuildable from `canonical_facts`. Replay is idempotent.
- API queries stay within the configured ClickHouse budgets and the read-only
  identity.

## Acceptance criteria

- **AC-1 Periods.** Discrete quarters, Q4 and TTM are derived as above. Missing inputs
  yield no derived point. 52/53-week periods are recognised. Instants become instant
  points with fiscal labels.
- **AC-2 Point in time.** `view="latest"`, `view="as_reported"` and `as_of` behave as
  described. Derived points respect the cutoff. `available_at` and `source_accessions`
  are correct.
- **AC-3 Acceptance time stored.** `facts_table(bundle)` has `accepted_at` from
  `bundle.filing.accepted_at`. `CanonicalFact.accepted_at` is set by the mapper.
- **AC-4 Concept priority.** The lowest `priority` wins for the same metric, period and
  filing, and the point records the winning concept.
- **AC-5 Consolidated only.** Dimensional observations never enter series.
- **AC-6 Formulas.** `free_cash_flow` is produced only where both operands exist.
- **AC-7 Storage and query.** `rebuild_metric_series` followed by
  `QueryService.metric_series` returns latest values, and values as of a cutoff, from
  ClickHouse.

## Implementation sequence

1. `accepted_at` through domain, Arrow schemas and DDL (AC-3). Bump
   `extraction_revision`, or backfill with a guarded `ALTER … UPDATE` from
   PostgreSQL (implementer's choice, documented).
2. `normalization/series.py` (AC-1, AC-2, AC-4 to AC-6). Pure functions, exhaustive
   unit tests.
3. `metric_series` table, `rebuild_metric_series`, `QueryService.metric_series`,
   API route (AC-7).
4. Worker trigger and docs. Validate suite including integration.
5. Operator check: AAPL operating cash flow for FY2016–FY2025. Confirm that the sum of
   the four derived quarters equals FY for each year.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/11-metric-series-point-in-time/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/11-metric-series-point-in-time/verification
```

### Baseline (main @ 8f16ca0, 5 October 2026)

All 13 tests fail:

- `normalization.series` and `query.series` don't exist.
- `facts_table` has no `accepted_at` column.
- `CanonicalFact` has no `accepted_at`.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- AC-3: `accepted_at` on `xbrl_facts` (from `bundle.filing.accepted_at`) and on
  `CanonicalFact` (set by the mapper), plus `concept_namespace`, `concept_qname`,
  `fiscal_year`, `fiscal_period`, `report_date` on canonical facts. Choice: filled by
  the stack's re-extraction at revision 3, no backfill; `db upgrade-clickhouse` now adds
  new Nullable columns to existing tables (NULL for old rows). The remap loader derives
  dei fiscal fields so remapped canonical facts carry them.
- AC-1/2/4/5/6: `normalization/series.py` (`MetricObservation`, `SeriesPoint`,
  `build_series`), selection before derivation, exact duration windows, YTD differences,
  Q4, contiguous TTM, fiscal labels via `filing_period_end`, formula metrics (simple
  `+ - * / ()` evaluator; default `free_cash_flow`, callers pass the YAML formulas).
- AC-7: DDL `045_metric_series.sql` (`metric_series`, `companies` mirror);
  `query/series.py` `rebuild_metric_series` writes `as_reported` rows and `latest`
  versions keyed by `known_from` (rebuilt as of each acceptance time); readers use the
  newest `built_at`. `QueryService.metric_series` / `cik_for_ticker`; API
  `GET /v1/series/{cik}` and `GET /v1/series?ticker=`. Observations come from each
  filing's latest published revision, or the newest canonical revision for filings
  without an extraction record. Missing acceptance time falls back to filed date.
- Worker: rebuild after completed `ingest_filing` and `map_canonical` (failures logged
  and reported in the result); company upserts also write the ClickHouse mirror. CLI
  `series rebuild --cik`.
- Gates: compileall, ruff, format, mypy, unit (390 passed, 1 skipped), scan_stubs (0),
  integration (36 passed), plan tests 01–11 (12/10/8/10/8/12/10/22/8/7/13 passed).

### Follow-up (verifier, 5 October 2026)

- Non-additive metrics: `CanonicalMetric.additive` (YAML `additive: false` on
  basic/diluted shares, shares outstanding, basic/diluted EPS; `dividends_per_share`
  stays additive). For them `build_series(non_additive=...)` keeps reported quarters,
  years, and instants only (no YTD differences, Q4 = FY - 9M, or TTM). The worker and
  `series rebuild` pass the catalogue's set; `DEFAULT_NON_ADDITIVE` mirrors it. TTM EPS
  is not derived. The flag isn't stored in PostgreSQL `canonical_metrics`.
- `metric_series` is a `ReplacingMergeTree(built_at)` keyed by point and `known_from`;
  rebuilds no longer issue a delete mutation; readers still use the newest build.
- SECURITY.md lists the new tables/views the API reader needs `SELECT` on.
- The verifier's probes (synthetic and real AAPL, temp databases) were rerun against
  this tree: no derived shares/EPS points; OCF quarters sum to FY; rebuild idempotent.

### Approved amendment (coordinator, 5 October 2026)

- New derivation `annual_minus_quarters`: Q4 = FY - (Q1 + Q2 + Q3), used only when
  the fiscal year has no nine-month year-to-date value (otherwise
  `annual_minus_nine_months` applies). Q1 starts on the fiscal year's start, Q1-Q3 are
  contiguous discrete quarters, Q4 starts the day after Q3 ends and spans a quarter,
  every input shares one unit, and the metric is additive. `source_accessions` and
  `available_at` cover all four inputs. Unit tests cover the fallback, the nine-month
  precedence case, a gap, mixed units, and a non-additive metric.
