# 02 — Fill fiscal year and period from the filing's dei facts

Status: implemented and merged to main.

## Problem

`fiscal_year` and `fiscal_period` are NULL on all 42,664 facts in `xbrl.xbrl_facts`
(measured 5 October 2026), even though every filing reports
`dei:DocumentFiscalYearFocus` and `dei:DocumentFiscalPeriodFocus`.

The facts table copies these fields from `bundle.filing` (`facts_table`,
[clickhouse.py:595](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/storage/clickhouse.py)). `bundle.filing` gets them from discovery
([filing_discovery.py:307](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/ingestion/filing_discovery.py)), which reads `fiscalYear` and `fiscalPeriod` keys
that the SEC submissions JSON doesn't publish. Nothing reads the dei facts. Quarterly
series, Q4 derivation and TTM all depend on these fields (see plan 11).

### Before / after

| Field | Before | After |
|---|---|---|
| `xbrl_facts.fiscal_year` (AAPL Q1 FY2020 10-Q) | NULL | 2020 |
| `xbrl_facts.fiscal_period` | NULL | `Q1` |
| `filings.fiscal_year` / `fiscal_period` in PostgreSQL | NULL | 2020 / `Q1` |
| `filings.report_date` when discovery didn't supply one | NULL | `dei:DocumentPeriodEndDate` |

## Scope

Likely files:

- `src/sec_xbrl_warehouse/ingestion/incremental.py`: derive the values after
  extraction (or after loading a saved extraction), before persisting.
- A small pure helper in `normalization/` (or `domain/`) that reads dei facts from a
  bundle and returns the derived values plus any validation issues.
- `docs/DATA_MODEL.md`: document where `fiscal_year`, `fiscal_period` and
  `report_date` come from.

Interfaces to keep:

- `IncrementalFilingIngestor.ingest(filing, extraction_revision=...)`.
- `ControlRepository.upsert_filing(filing)`: the enriched `FilingRef` is persisted
  through this existing port. No new port method.
- `facts_table(bundle)` keeps reading `bundle.filing`. The ingestor passes an
  enriched bundle to `insert_xbrl_bundle` and the Parquet writer.

Exclusions:

- No change to fact identity (`fact_id`, `context_hash`, `duplicate_set_hash`).
- Calendar inference from dates is out of scope. A value is either taken from dei or
  left NULL.
- Bulk FSNDS rows already carry `fy` and `fp` from the SEC and are unaffected.

Invariants:

- Raw dei facts are stored unchanged. Only the filing-level denormalized columns
  change.
- Replay is deterministic: the same bundle always yields the same values, whether it
  comes from a fresh extraction or the saved extraction checkpoint.

## Rules

- Only facts in a dei namespace (`http://xbrl.sec.gov/dei/<year>`) with **no
  dimensions** are considered. Dimensional dei facts (co-registrants) are ignored.
- `fiscal_year`: the integer value of `DocumentFiscalYearFocus` (`gYear`, which Arelle
  returns as text such as `"2025"`).
- `fiscal_period`: `DocumentFiscalPeriodFocus`, trimmed and upper-cased. Allowed values
  are the dei enumeration: `FY, Q1, Q2, Q3, Q4, H1, H2, M9, T1, T2, T3, M8, CY`.
- `report_date`: `DocumentPeriodEndDate` (`value_date`), used only when discovery
  supplied none.
- Precedence: a dei value replaces a different discovery value, and a warning is
  recorded.

## Acceptance criteria

- **AC-1 Population.** After `ingest`, every fact row built from the persisted bundle
  carries the dei `fiscal_year` and `fiscal_period`. The final `upsert_filing` call
  carries the same values, plus `report_date` when discovery lacked one. This works
  with the fact shapes real Arelle produces (fixture `fixtures/dei-inline`). A
  discovery-supplied `report_date` is never overwritten.
- **AC-2 Precedence.** If the discovery `FilingRef` has different values, the dei
  values win. One `warning` issue with code `warehouse:fiscalFocusMismatch`, the
  filing's accession, and both values in `details` goes to `record_validation_issues`.
- **AC-3 Dimensional facts.** Dei facts with dimensions never affect the result and
  never raise a conflict.
- **AC-4 Conflicts.** Two or more non-dimensional facts for the same dei concept with
  different values: that field is NULL, an `error` issue
  `warehouse:fiscalFocusConflict` is recorded, and ingestion still completes. Error
  severity isn't in the default reject list.
- **AC-5 Normalization.** `fy` becomes `FY` and `" q3 "` becomes `Q3`. A value outside
  the enumeration (for example `Q5`) gives NULL plus a `warning`
  `warehouse:fiscalFocusInvalid`. The other field is unaffected.
- **AC-6 Absence.** Without dei facts, the discovery values pass through unchanged
  (including NULL) and no `warehouse:fiscal*` issue is recorded.

## Implementation sequence

1. Pure helper: bundle → (fiscal_year, fiscal_period, report_date, issues).
2. Call it in `ingest` after the bundle is available, both from fresh extraction and
   from the saved extraction checkpoint. Replace `bundle.filing` with the enriched
   `FilingRef`, record the issues, and upsert the enriched filing.
3. Bump `extraction_revision` (share the bump with plans 01 and 03 if they ship
   together), update `DATA_MODEL.md`, run the Validate suite.
4. Operator step: re-extract the AAPL filings. Confirm
   `SELECT countIf(fiscal_year IS NULL) FROM xbrl.v_xbrl_facts_latest` is 0.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/02-dei-fiscal-fields/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/02-dei-fiscal-fields/verification
```

The `arelle` test needs the local Arelle cache (`data/cache/arelle`). The fixture's
`dei-2024.xsd` is a minimal local schema in the real dei namespace, so no SEC
taxonomy download is needed.

### Baseline (main @ 8f16ca0, 5 October 2026)

- 9 failed, 1 passed.
- Every AC-1 to AC-5 test fails on its assertion: rows show `(None, None)` or the
  discovery `(2024, 'Q4')`.
- `test_ac6_missing_dei_keeps_discovery_values` passes on baseline. It pins existing
  correct behavior that the change must keep.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- Helper `normalization/fiscal_focus.py::derive_fiscal_focus(bundle)` returns the
  enriched `FilingRef` and `warehouse:fiscalFocus*` issues (AC-2 to AC-6).
- `IncrementalFilingIngestor.ingest` applies it after a fresh or checkpointed
  extraction, before the validation stage: it replaces `bundle.filing`, appends the
  issues to `bundle.validation_issues` (recorded once under the validation
  checkpoint), and calls `upsert_filing` with the enriched filing (AC-1). The saved
  extraction stays as extracted.
- Conflicting or invalid dei values set the field to NULL even if discovery had a value.
- Revision: shares `CURRENT_EXTRACTION_REVISION = 3` from plan 01.
- Gates: compileall, ruff, format, mypy, unit (194 passed), scan_stubs (0),
  integration (23 passed), plan 01 tests (12 passed), plan 02 tests (10 passed).
- Not done: re-extracting AAPL and checking `countIf(fiscal_year IS NULL) = 0` (operator step).
