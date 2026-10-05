# 10 — Financial statements as presented (income, balance sheet, cash flow)

Status: plan only. Code follows after this plan is reviewed.

## Problem

The warehouse stores every fact and every presentation relationship, but it can't
reproduce a company's primary statements:

- **Role definitions aren't captured.** Each statement is an extended link role with
  a definition such as `100000 - Statement - Consolidated Balance Sheets`. The bundle
  keeps only the role URI (`xbrl_relationships.link_role_uri`). Definitions say
  whether a role is a primary statement, a parenthetical, or a disclosure, and give
  the filer's own ordering code.
- **No statement classification, no line ordering.** Nothing turns presentation
  arcs into ordered lines with depth, labels (including preferred total labels) and
  values per period.

The research pipeline and any analyst view need the income statement, balance sheet
and cash flow statement as the company presented them. Standardized cross-company
values are plans 06 and 11. This layer is the as-presented one.

### Before / after (fixture `fixtures/statements`)

| | Before | After |
|---|---|---|
| Role `…/BalanceSheet` | URI only | definition, `sort_code=100000`, `category=statement` |
| Which roles are primary statements | unknown | `{BalanceSheet: balance_sheet, IncomeStatement: income_statement, CashFlow: cash_flow}`; parenthetical and disclosure roles excluded |
| Balance sheet | scattered facts | 11 ordered lines; "Total assets" at depth 2 with 2025 and 2024 values |

## Scope

Likely files:

- `domain/xbrl.py`:
  - `XbrlRole(role_uri, definition, used_on, sort_code, category)`;
  - `XbrlExtractionBundle.roles: tuple[XbrlRole, ...] = ()`. The default keeps older
    extraction JSON readable.
- `infrastructure/xbrl/arelle_adapter.py`: read `modelXbrl.roleTypes` into `roles`.
  `category` is the middle token of the SEC definition convention
  (`Statement`, `Disclosure`, `Document`, otherwise `other`), lower-cased.
- New `normalization/statements.py`: `StatementKind` (`balance_sheet`,
  `income_statement`, `comprehensive_income`, `cash_flow`, `equity`),
  `classify_roles(bundle)` and `build_statement_lines(bundle)`. The returned
  `StatementLine` objects carry `accession, role_uri, statement_kind, line_number,
  depth, concept_qname, label, preferred_label_role, is_abstract, is_total, values`.
  `StatementValue` carries `fact_id, period_start, period_end, period_instant,
  value_decimal`.
- `infrastructure/storage/arrow_schemas.py`, `clickhouse.py`, and new DDL in
  `sql/clickhouse/` for:
  - an `xbrl_roles` table;
  - an `xbrl_statement_lines` table (one row per line per value, or one row per
    line with arrays — implementer's choice, documented);
  - a `v_statement_lines_latest` view filtered to the latest published revision,
    like `v_xbrl_facts_latest`.
- `ingestion/incremental.py`: write roles and lines with the bundle, inside the
  existing checkpointed, publish-marker flow.
- `docs/DATA_MODEL.md`, `docs/ARCHITECTURE.md` (gold layer), with an example query.

### Classification rules

1. Prefer the role definition: `category == "statement"`, excluding titles containing
   "Parenthetical". The title decides the kind: balance sheet / financial position /
   financial condition; operations / income / earnings; comprehensive income; cash
   flows; equity / shareholders' / stockholders'.
2. When definitions are missing (older extractions), fall back to the role URI's last
   segment with the same keywords.
3. Disambiguate with anchor concepts present in the role's presentation tree. Examples:
   `Assets` or `LiabilitiesAndStockholdersEquity` (balance sheet);
   `NetCashProvidedByUsedInOperatingActivities` (cash flow). If several roles claim
   the same kind, keep all and record which one has the anchors.

### Line rules

- Pre-order walk of the role's parent-child arcs by `order`. `line_number` starts
  at 1. Depth counts from the root (depth 0).
- Label: the concept's label in the arc's `preferredLabel` role if present, otherwise
  the standard label. `is_total` is true when `preferredLabel` is the total label
  role.
- Values: facts for the concept with **no dimensions** and `duplicate_ordinal == 1`.
  One value per period. Lines without facts keep an empty `values`.

Exclusions:

- Dimensional statement tables (the equity statement's components axis, segment
  breakdowns) are plan 12. The equity statement is classified but its dimensional
  cells aren't populated here.
- Standardization across companies is plans 06 and 11.

Invariants:

- Raw relationships and facts are unchanged. Lines are derived and rebuildable from
  silver.
- Replay of the same accession and revision produces identical lines (no duplicate
  logical rows).

## Acceptance criteria

- **AC-1 Roles.** Extraction returns every role type with its definition, the
  `sort_code` parsed from the leading digits, and `category` (`statement`,
  `disclosure`, …).
- **AC-2 Classification.** `classify_roles` returns exactly the primary statements:
  no parenthetical, disclosure or segment-detail roles. It works without role
  definitions by falling back to the URI plus anchor concepts.
- **AC-3 Lines.**
  - `build_statement_lines` orders lines by presentation and numbers them 1..n per
    role.
  - It applies preferred total labels, sets depth, and attaches one non-dimensional,
    first-ordinal value per period.
  - A concept presented on two statements (net income on the income statement and
    the cash flow statement) appears on both.
  - Lines without facts have empty values.
- **AC-4 Storage.** After `insert_xbrl_bundle` and `publish_xbrl_bundle`,
  `v_statement_lines_latest` returns the lines for the accession in presentation
  order, for the latest published revision only.

## Implementation sequence

1. `XbrlRole` and Arelle extraction (AC-1).
2. `normalization/statements.py` (AC-2, AC-3). Pure functions with unit tests.
3. Arrow schemas, DDL and inserts (AC-4). `db init-clickhouse` creates the new tables
   compatibly. Add the new tables to `doctor`'s expected list.
4. Ingest wiring. Bump `extraction_revision` (roles are new extraction output).
5. Docs. Validate suite including integration. Operator step: re-extract AAPL, then
   compare `v_statement_lines_latest` for the FY2025 10-K with the filing's balance
   sheet (R2) line by line.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/10-financial-statement-lines/acceptance
RUN_XBRL_INTEGRATION=1 .venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/10-financial-statement-lines/verification
```

`fixtures/statements/` is a self-contained filing with five roles (balance sheet,
parenthetical, operations, cash flows, a disclosure), total labels and two periods.

### Baseline (main @ 8f16ca0, 5 October 2026)

All 7 tests fail:

- AC-1: the bundle has no `roles`.
- AC-2 and AC-3: `normalization.statements` doesn't exist.
- AC-4: `v_statement_lines_latest` doesn't exist.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
