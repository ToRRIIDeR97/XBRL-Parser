# 12 — Cover page, segment and geographic breakdowns, and text blocks

Status: plan only. Code follows after this plan is reviewed. Benefits from plans 02
and 03 (correct dei values) and plan 10 (roles).

## Problem

Three kinds of information are already in `xbrl_facts` but aren't usable without
writing custom SQL against raw dimensions:

1. **Cover page (dei).** Document type, period end, registrant, filer category,
   amendment flag, ticker, exchange, auditor, shares outstanding and public float.
   Shares outstanding is often reported per share class, on the class-of-stock axis,
   and needs summing.
2. **Segment, product and geographic breakdowns.** Dimensional facts on the
   segment, product/service, geographic and consolidation-items axes. The research
   pipeline needs segment revenue and profit, geographic revenue, and both versions
   when segments are recast. Member labels (`AmericasSegmentMember` → "Americas") are
   in concept labels. Plain consolidated facts have no member.
3. **Footnote and policy text blocks.** About 4,500 text facts are stored (AAPL), for
   example `RevenueRecognitionPolicyTextBlock` and `SegmentReportingDisclosureTextBlock`.
   There's no view that lists them per filing or separates accounting policies from
   other disclosures.

### Before / after

| | Before | After |
|---|---|---|
| "Apple FY2025 Americas revenue" | SQL over `dimensions_json` and concept labels | `v_segment_facts_latest`: `segment` / `Americas` / 178,000 |
| Shares outstanding with two share classes | two dimensional facts | `cover_page.shares_outstanding` = their sum, with the as-of date |
| Accounting policies for a filing | search `concept_name LIKE '%TextBlock'` | `v_text_blocks_latest WHERE is_policy` |

## Scope

Likely files:

- New `normalization/disclosures.py`:
  - `cover_page(bundle) -> CoverPage` with fields `document_type, period_end_date,
    registrant_name, filer_category, amendment, trading_symbol, exchange,
    auditor_name, shares_outstanding, shares_outstanding_date, public_float`;
  - `segment_rows(bundle) -> tuple[SegmentRow, ...]`. `SegmentRow` carries
    `concept_qname, axis, member_label, members, value, unit, period_start,
    period_end, period_instant, is_elimination, fact_id`, where `members` is a tuple of
    `SegmentMember(axis, qname, label)`.
- `sql/clickhouse/`, `arrow_schemas.py`, `clickhouse.py`:
  - `xbrl_cover_pages` and `xbrl_segment_facts` tables, written with the bundle;
  - views `v_cover_pages_latest`, `v_segment_facts_latest` and `v_text_blocks_latest`,
    all filtered to the latest published revision. The text-block view is a view over
    `xbrl_facts` with `is_policy` set for `…PolicyTextBlock` concepts.
- `ingestion/incremental.py`: write the new tables in the existing checkpointed flow.
- `query/service.py` and the API: optional read endpoints, using the same budgets.
- `docs/DATA_MODEL.md`, with example queries.

Rules:

- **Cover page.**
  - Only dei facts without dimensions, except shares outstanding, which sums the
    class-of-stock members when no undimensioned value exists. `shares_outstanding_date`
    is the instant.
  - Facts on the legal-entity axis (co-registrants) are ignored.
  - A missing fact gives `None`.
- **Breakdown axes (allowlist).**

  | Axis | Kind |
  |---|---|
  | `us-gaap:StatementBusinessSegmentsAxis` | `segment` |
  | `srt:ProductOrServiceAxis` | `product` |
  | `srt:StatementGeographicalAxis` | `geography` |
  | `srt:ConsolidationItemsAxis` | `consolidation` |

  - A fact qualifies only when **all** its dimensions are on allowlisted axes.
  - `axis` and `member_label` describe the first member by the order segment, product,
    geography, consolidation. `members` lists all of them.
  - `is_elimination` is true for intersegment elimination members.
- **Labels.** Labels come from the bundle's concepts (standard label). Without a
  label, use the member's local name minus the `Member` suffix
  (`USMember` → `US`).

Exclusions:

- Interpreting text-block HTML (stripping or summarizing) is out of scope. Text is
  stored as filed.
- Proxy (DEF 14A) executive-pay data and narrative MD&A extraction are separate future
  plans.

Invariants:

- Everything is derived from stored raw facts and is rebuildable from silver. Raw
  rows are unchanged.
- Recast segments stay visible per filing (each accession keeps its rows). The latest
  views filter by revision only, never across filings.

## Acceptance criteria

- **AC-1 Cover page.** `cover_page` maps the dei facts as listed. It sums share
  classes, ignores co-registrant facts, and returns `None` for absent facts.
- **AC-2 Breakdowns.** `segment_rows` returns only facts whose dimensions are all on
  allowlisted axes, with the axis kind and member labels (fallback as above).
  - Consolidated facts and other axes (for example `RestatementAxis`) are excluded.
  - Multi-axis facts keep every member.
  - Eliminations are flagged.
- **AC-3 Text blocks.** `v_text_blocks_latest` lists every `…TextBlock` fact of the
  latest revision with `is_policy` true for policy text blocks.
- **AC-4 Storage.** After `insert_xbrl_bundle` and `publish_xbrl_bundle`,
  `v_cover_pages_latest` and `v_segment_facts_latest` return the bundle's cover page
  and breakdown rows.

## Implementation sequence

1. `normalization/disclosures.py` (AC-1, AC-2). Unit tests.
2. DDL, Arrow schemas, writes and views (AC-3, AC-4). Add them to `doctor`'s expected
   objects.
3. Ingest wiring. Bump `extraction_revision`, or backfill from silver.
4. Optional API endpoints. Docs. Validate suite including integration.
5. Operator check: AAPL FY2025 segment revenue (Americas, Europe, Greater China,
   Japan, Rest of Asia Pacific) sums to consolidated net sales.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/12-disclosure-views/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/12-disclosure-views/verification
```

### Baseline (main @ 8f16ca0, 5 October 2026)

All 7 tests fail: `normalization.disclosures` doesn't exist, and
`v_cover_pages_latest` is an unknown table. The synthetic bundle inserts into the
existing tables without error.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.

### Implementer record (5 October 2026)

- `normalization/disclosures.py`: `cover_page` and `segment_rows` (`CoverPage`,
  `SegmentRow`, `SegmentMember` with `axis` = breakdown kind). Axes match by name and
  standard namespace family, so look-alike namespaces never qualify. Several values for
  one cover field: first in document order. Share classes: one value per class member
  at the latest instant. Eliminations: members named `…Elimination(s)Member`.
  Duplicate facts (ordinal > 1) and nil facts are skipped, as in statement lines.
- `sql/clickhouse/027_disclosure_tables.sql` (`xbrl_cover_pages`,
  `xbrl_segment_facts`, ReplacingMergeTree by revision) and three views in
  `040_views.sql`. `cover_pages` and `segment_facts` are derived `XBRL_ENTITIES`, so
  they get silver checkpoints, ClickHouse inserts, and publish row-count checks with
  the bundle. Completed jobs without these checkpoints still resume.
- Revision: no bump (the single `CURRENT_EXTRACTION_REVISION = 3` policy); filings
  extracted at revision 3 get the new rows, older completed extractions get them on
  re-extraction.
- `doctor` expects the three views; SECURITY.md lists the `xbrl_api` grants;
  DATA_MODEL.md and ARCHITECTURE.md document the tables and example queries.
- Optional API endpoints were not added.
- Operator check (step 5) is pending a revision-3 AAPL extraction.

### Follow-up (verifier, 5 October 2026)

- Multi-security filers report `TradingSymbol`, `SecurityExchangeName`, and
  `Security12bTitle` per security on `us-gaap:StatementClassOfStockAxis`. `cover_page`
  now keeps every such security (`CoverPage.securities`; arrays `security_members`,
  `security_titles`, `security_symbols`, `security_exchanges` on `xbrl_cover_pages`)
  and, without a non-dimensional ticker/exchange, takes both from one security: the one
  matching a non-dimensional ticker, else common stock (`CommonStockMember` or a title
  containing "Common Stock"), else the first listed. The verifier's AAPL probe (temp
  databases) gives `AAPL` / `NASDAQ` and eight securities.
