# Data Model

## Source fidelity principle

The raw layer stores what was filed or what the SEC bulk set published. The canonical layer stores an interpretation. These are separate records with explicit lineage.

## SEC FSNDS tables

The bulk loader expects eight tab-delimited tables.

TSV fields use CSV quoting: quoted tabs, escaped double quotes, and quoted
newlines are field content. The reader bounds bytes per logical record and
counts only unquoted separators before allocating CSV fields. Checkpoints
retain the physical source line where each batch begins.

### SUB — submissions

Primary key: `adsh`.

Contains accession, CIK, registrant identity/address, filer status, form, fiscal metadata, filing/acceptance dates, instance filename, co-registrant metadata, and public-float fields.

### TAG — taxonomy tags

Compound key: `(tag, version)`.

Contains standard and extension tag metadata: custom/abstract flags, datatype, instant-or-duration indicator, credit/debit balance, label, and documentation.

### DIM — dimensions

Primary key: `dimh`.

Contains the SEC-provided hash and lexical representation of axis/member pairs. `dimh = 0` represents no XBRL dimensions.

### NUM — numeric facts

Compound key: `(adsh, tag, version, ddate, qtrs, uom, dimh, iprx)`.

Preserve both the raw decimal text and a parsed decimal value. Never use binary floating point as the only representation.

NUM and TXT `ddate` remain typed dates in Arrow/Parquet. ClickHouse stores their
ISO date strings so published years outside its native Date/Date32 ranges are
retained without clamping. The adapter converts dates to strings only on the
ClickHouse transport boundary. Invalid calendar dates still fail parsing;
unusual but parseable dates are preserved, not corrected to an assumed year.

### TXT — text facts

Compound key: `(adsh, tag, version, ddate, qtrs, dimh, iprx, _row_hash)`.

The source-row hash preserves distinct contexts and other published text values
that share the SEC business fields. Replaying the same row remains idempotent.

Contains normalized plain text and metadata including language, source length, truncation information, context reference, and footnotes.

### REN — rendering groups

Compound key: `(adsh, report)`.

Describes SEC rendering/menu groups and role hierarchy.

### PRE — presentation lines

Compound key: `(adsh, report, line)`.

Describes statement location, order, indentation/parenthetical status, preferred labels, and negation behavior.

### CAL — calculation relationships

Compound key: `(adsh, grp, arc)`.

Describes parent-child calculation arcs and sign/weight direction.

Preserve the published `negative` integer: current signed weights (-1/1) and
older 0/1 encodings remain distinct source values, not normalized booleans.
Arrow and ClickHouse use signed Int16 so upgrades preserve the full former
UInt8 range. Parsing accepts only -1, 0, or 1. Interpret direction using the
source encoding; do not treat both nonzero values as a negative arc.

## Full-fidelity Arelle entities

### `xbrl_facts`

Required identity and lineage:

- `fact_id`: deterministic hash of accession, source document, inline ID or fact path, concept, context, unit, and duplicate ordinal.
- accession and filing metadata.
- concept namespace, local name, QName, taxonomy version.
- context and unit IDs.
- raw lexical value.
- typed decimal/text/date/boolean value where applicable.
- nil, decimals, precision, language.
- period start/end/instant.
- entity scheme and identifier.
- dimension JSON plus stable context/dimension hashes.
- footnote IDs and tuple ancestry.
- source document, source fact ID, extraction revision.

### Filing fiscal fields

`fiscal_year`, `fiscal_period`, and `report_date` on `xbrl_facts` and on the
PostgreSQL `filings` row are filing-level values denormalized from the filing's own
dei facts (`normalization/fiscal_focus.py`). The SEC submissions JSON doesn't publish
them, so discovery usually leaves them NULL.

- Only non-nil, non-dimensional facts in a `http://xbrl.sec.gov/dei/<version>`
  namespace count. Dimensional (co-registrant) dei facts are ignored.
- `fiscal_year` is the integer `dei:DocumentFiscalYearFocus`.
- `fiscal_period` is `dei:DocumentFiscalPeriodFocus`, trimmed and upper-cased, and
  must be one of `FY, Q1, Q2, Q3, Q4, H1, H2, M9, T1, T2, T3, M8, CY`.
- `report_date` is `dei:DocumentPeriodEndDate`, used only when discovery supplied none.
- A dei value replaces a different discovery value and records a
  `warehouse:fiscalFocusMismatch` warning. Different values for the same concept
  leave the field NULL with a `warehouse:fiscalFocusConflict` error. A value outside
  the enumeration or not a year leaves it NULL with a `warehouse:fiscalFocusInvalid`
  warning. Without dei facts, the discovery value passes through.
- Raw dei facts and the saved extraction checkpoint are unchanged. The values are
  derived again on replay, so they are deterministic. No date-based calendar
  inference is done. Bulk FSNDS rows keep the SEC's own `fy` and `fp`.
- The discovery upsert at the start of a re-ingestion
  (`upsert_filing(..., keep_derived=True)`) never replaces a stored `fiscal_year`,
  `fiscal_period`, or `report_date` with NULL. The post-extraction upsert writes the
  derived values as they are, including NULL for conflicting or invalid dei values,
  so PostgreSQL and the fact rows agree. A period that differs from discovery only in
  case isn't a mismatch.

### `xbrl_contexts`

One row per context per extraction revision. Includes entity, period, dimensional scenario/segment content, and a canonical context hash.

### `xbrl_concepts`

One row per QName/taxonomy version used by the filing or loaded taxonomy. Includes datatype, substitution group, period type, balance, abstract/nillable flags, labels, documentation, and extension status.

### `xbrl_relationships`

One row per arc. Includes network type, link role, arc role, source/target QName, order, priority, weight, preferred label, and dimensional flags.

## Companies

`companies` in PostgreSQL (migration `0004_companies`) holds one row per CIK from
the SEC submissions JSON: `name`, `entity_type`, `sic`, `sic_description`, `tickers`
and `exchanges` (text arrays in source order, tickers upper-cased),
`fiscal_year_end` (`MMDD`, or NULL when missing or not a real date),
`state_of_incorporation`, `filer_category`, `former_names` (JSON list of
`{name, valid_from, valid_to}`), `source_checksum` (hash of the submissions
document), and `fetched_at`. EIN, phone, and addresses are not stored.

- `tickers` and `exchanges` are parallel lists, as in the SEC submissions JSON
  (a missing exchange is `""`). A ticker belongs to at most one company: an upsert that
  lists a ticker removes it, and the exchange at the same position, from any other
  company in the same transaction.
- Ticker lookups are case-insensitive.
- Rows come from `filing discover --cik` (the same submissions request as the
  filings) and, for daily-index discovery, from one submissions request per CIK not
  yet stored or fetched more than `XBRL_COMPANY_REFRESH_DAYS` (default 30) days ago
  (wall-clock time, so backfills of old dates refresh too). `--cik` discoveries always refresh the row.

## Validation issues

`validation_issues` stores diagnostics in PostgreSQL. Filing extractions record
issues tagged with `extraction_revision` (NULL for bulk loads and for rows written
before migration `0002_validation_issue_revision`). Repeated issues from one
extraction that share `(source, code, severity, source_document)` are stored as one
row: the first occurrence's message, line, and column, plus `details.occurrences`
and up to five `details.samples` (line, column, message, details). Fatal issues are never
grouped. The reject decision still uses every individual issue.

### `xbrl_roles`

One row per extended link role the filing declares or uses: `role_uri`,
`definition` (for example `100000 - Statement - Consolidated Balance Sheets`),
`used_on`, `sort_code` (leading digits), `category` (`statement`, `disclosure`,
`document`, or `other`, from the SEC "<code> - <Category> - <Title>" convention), and
`statement_kind` when the role is a primary statement.

### `xbrl_statement_lines` and `v_statement_lines_latest`

Primary financial statements as the filer presented them, derived by
`normalization/statements.py` from roles, presentation arcs, and facts (rebuildable
from silver; raw relationships and facts are unchanged).

- **Classification.** A role is a primary statement when its definition's category is
  `statement` and its title isn't a parenthetical; the title gives the kind
  (`balance_sheet`, `income_statement`, `comprehensive_income`, `cash_flow`, `equity`).
  Without a definition (older extractions) the role URI's last segment decides, the URI
  must not name details/tables/policies/parenthetical, and the role must contain an
  anchor concept (for example `Assets`, `NetCashProvidedByUsedInOperatingActivities`).
  Several roles of one kind are all kept.
- **Lines.** One row per line: a pre-order walk of the role's parent-child arcs by
  `order` (`line_number` from 1 per role, `depth` 0 at the root). `label` uses the
  arc's preferred label role, else the standard label; `is_total` marks the total
  label role. A concept presented on two statements appears on both.
- **Values.** Parallel arrays `value_fact_ids`, `value_period_starts`,
  `value_period_ends`, `value_period_instants`, `value_decimals`: one entry per
  period, from the concept's non-dimensional, first-ordinal facts, latest period
  first. Lines without facts have empty arrays. Dimensional statement tables (equity
  components, segments) are not populated here.
- `v_statement_lines_latest` shows only each accession's latest published revision.

```sql
SELECT line_number, depth, label, is_total, value_period_instants, value_decimals
FROM xbrl.v_statement_lines_latest
WHERE accession = '0000320193-25-000079' AND statement_kind = 'balance_sheet'
ORDER BY role_uri, line_number;
```

## Canonical layer

### `canonical_metrics` in PostgreSQL

Defines the stable metric code, expected statement, period type, unit family, and optional derived formula.

### `concept_mappings` in PostgreSQL

Append-only/versioned mapping decisions. A mapping can be:

- exact taxonomy version + QName;
- namespace + local name with validity range, where the namespace is an exact URI or
  a family token (`us-gaap`, `ifrs-full`, `dei`, `srt`) covering every standard year;
- filing-specific extension mapping;
- rule-based candidate requiring review.

Specificity order: CIK, taxonomy version, exact namespace URI, family. See
CANONICAL_MAPPING.md.

### Fact `taxonomy_version`

`xbrl_facts.taxonomy_version` is the date or year in the concept's namespace
(`http://fasb.org/us-gaap/2025` gives `2025`). Before extraction revision 3 it was
read from the namespace of the concept's XML Schema declaration, so every fact
showed `2001`; facts and reviews written at revisions 1–2 carry that value.

### `canonical_facts` in ClickHouse

Contains:

- raw fact ID;
- canonical metric code;
- normalized decimal/text value;
- normalized unit and sign;
- mapping ID/version/confidence/explanation;
- dimensional scope;
- source and extraction revision.

## Row hashing

Every silver/raw warehouse row should have a deterministic hash over normalized field serialization. Hashing excludes ingest timestamps but includes all source-controlled values. This supports replay verification and source-revision comparison.
