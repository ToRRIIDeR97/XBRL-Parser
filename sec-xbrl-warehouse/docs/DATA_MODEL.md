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

**ClickHouse encoding.** Silver Parquet keeps every value as extracted (hex hashes,
every raw value, every concept; standard concepts in shared files, see below). ClickHouse stores the same data more compactly; the
`v_*` views and `QueryService` return it in the original form:

- **Hashes.** Identity hashes (`fact_id`, `context_hash`, `dimension_hash`,
  `duplicate_set_hash`, `footnote_ids`, `relationship_id`, `footnote_id`,
  `linked_fact_ids`, `canonical_fact_id`, `raw_fact_id`, segment `fact_id`,
  statement-line `value_fact_ids`) and every `_row_hash` are the same SHA-256 values
  stored as 32 raw bytes (`FixedString(32)`). The views return `lower(hex(...))` under
  the same column names, so query results and API responses are unchanged. A tuple
  fact's blank `context_hash` is stored as zero bytes and read back as `''`. Querying
  a raw table directly needs `lower(hex(col))` or `unhex('<hex>')`. SEC bulk tables keep
  their hex `_row_hash`.
- **Non-hex identifiers (test-only).** The pipeline only produces hex SHA-256
  identifiers. Some test fixtures use other strings (`"rev"`); those are stored as the
  SHA-256 of the text, identically on insert and upgrade, so joins still match, but the
  views then return that digest rather than the original text. Outside
  `XBRL_ENV=test` this logs `non_hex_identifier_hashed`.
- **Raw values.** `xbrl_facts.raw_value` is NULL when it equals the typed value, and
  `raw_value_scale` records how to rebuild it: for numeric facts, the fraction digits
  of `str(value_decimal)` in plain notation (`"1000"` → 0, `"1.50"` → 2); for text
  facts equal to `value_text`, 0. Formatted numbers (`"1,234"`, `"(5)"`), HTML text
  blocks, nil, boolean, and date facts, and facts without a typed value keep
  `raw_value`. `v_xbrl_facts_latest.raw_value` rebuilds the exact string.

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
- These rules are the SEC adapter's. Each source adapter derives the fields
  (`SourceRegistry.fiscal_focus`); ESEF's, EDINET's and DART's rules are under their
  sections below.

### `xbrl_contexts`

One row per context per extraction revision. Includes entity, period, dimensional scenario/segment content, and a canonical context hash.

### `xbrl_concepts`, `taxonomy_concepts`, and `v_filing_concepts_latest`

Concept rows include datatype, substitution group, period type, balance,
abstract/nillable flags, labels, documentation, and extension status.

- `xbrl_concepts` holds each filing's **extension** concepts (per accession and
  revision).
- `taxonomy_concepts` holds standard concepts (us-gaap, dei, srt, ...) once per
  `(concept_namespace, concept_name, taxonomy_version)`.
- **Labels.** SEC filings don't load the standard label linkbases, so most labels a
  bundle carries for a standard concept come from the filer's own label linkbase (for
  example AAPL's terse label "Net sales"). A concept counts as filer-labelled when the
  filing has a label arc for it in one of its own documents (`xbrl_relationships`,
  `network_type = 'label'`, `source_document` in the extraction's
  `source_documents`). Its full label set is stored for that filing only in
  `xbrl_concept_labels (accession, extraction_revision, concept_qname, labels_json)`,
  and `taxonomy_concepts` gets `labels_json = '[]'`, `labels_rank = 0` for it. Other
  standard concepts' labels are taxonomy-owned and shared (`labels_rank = 1`;
  `ReplacingMergeTree(labels_rank)`, so `FINAL` keeps a labelled row).
- `v_filing_concepts_latest` lists, per accession (latest revision), its extension
  concepts plus the standard concepts its facts use (matched on namespace and name),
  with the filing's own labels where it has them, else the shared ones. No filer's
  labels reach another filing.

Silver Parquet holds every concept of every filing, each stored once:

- The filing's `silver/xbrl/concepts/.../accession=<a>/revision=<r>/` objects hold
  its extension concepts and the standard concepts it labels itself (with the
  filer's labels), one row per concept as before.
- Every other standard concept is stored as a `taxonomy_concepts` row (no accession,
  no ingest time, rows sorted by name) under
  `silver/xbrl/taxonomy_concepts/namespace_sha256=<first 16 hex of the namespace's
  SHA-256>/taxonomy_version=<v>/`. The objects are content-addressed, so filings that
  load the same taxonomy share one object.
- The filing's `silver:concepts` checkpoint lists both sets, and its row count is
  the filing's concept count. The saved extraction keeps the full concept list too.
- Filings written before this change keep a full per-filing copy.

### `xbrl_relationships`

One row per arc. Includes network type, link role, arc role, source/target QName, order, priority, weight, preferred label, and dimensional flags.

## Filing keys and entity keys

The `accession` and `cik` columns (PostgreSQL, ClickHouse, Parquet) and API fields
keep their names, but hold source-neutral keys (`domain/sources.py`, migration
`0008_filing_entity_keys`). Every SEC value is unchanged.

- **Filing key** (`accession`): an SEC accession (`0000320193-25-000079`) or
  `<source>-<native id>` for any other source, for example
  `esef-5493001KJTIIGC8Y1R12-2023-12-31-ESEF-GB-0`. `source` is `[a-z][a-z0-9]{1,15}`
  and the native id `[A-Za-z0-9][A-Za-z0-9._-]*`, 128 characters at most in total.
  SEC accessions start with a digit, so the forms never overlap, and `sec-...` is
  invalid. `filing_source(key)` returns `sec` or the prefix and raises `ValueError`
  for anything else. No `source` column is stored; it is read from the key.
- **Entity key** (`cik`): the SEC CIK for SEC issuers (every CIK is below 10^10) and a
  number at or above `ENTITY_KEY_BASE = 10_000_000_000` for everything else.
  `FilingRef` checks the range against the filing's source.
- **`entity_keys`** (PostgreSQL): `entity_key` (from `entity_key_seq`, starting at
  10^10), `scheme`, `identifier` (unique together, for example the LEI scheme
  `http://standards.iso.org/iso/17442` and an LEI), `created_at`.
  `PostgresControlRepository.resolve_entity_key(scheme, identifier)` returns the CIK
  for `SEC_CIK_SCHEME` (`http://www.sec.gov/CIK`) without a row and otherwise the
  existing or a newly allocated key, the same one for concurrent first calls. Keys are
  never reused or renumbered, and SEC CIKs are never written here.
- **Object keys** carry the source: `bronze/<source>/filings/<entity key>/<filing
  key>/...` and `manifests/<source>/filings/<filing key>/...`, so SEC keys stay
  `bronze/sec/...` and `manifests/sec/...`.

## ESEF filings

ESEF annual reports come from XBRL International's filings.xbrl.org
(`sources/esef.py`, source code `esef`). Each filings.xbrl.org record becomes:

| Field | Value |
|---|---|
| `accession` (filing key) | `esef-<fxo_id>`, e.g. `esef-213800H2PQMIF3OVZY47-2023-12-31-ESEF-GB-0`; a record whose key isn't valid is skipped and logged |
| `cik` (entity key) | `resolve_entity_key("http://standards.iso.org/iso/17442", <LEI>)`; the LEI is trimmed and upper-cased first, so one issuer has one key |
| `company_name` | the included entity's `name` (the LEI if it has none) |
| `form` | `ESEF` |
| `filed_date` | the date of `date_added` (when filings.xbrl.org added the report) |
| `report_date` | `period_end` |
| `archive_directory_url` | `https://filings.xbrl.org` + the directory of `package_url` |
| `primary_document_url` | `https://filings.xbrl.org` + `report_url` |
| `is_inline_xbrl` | true |
| `fiscal_period`, `fiscal_year` | `FY` and `report_date`'s year; without a report date, the end year of the latest non-nil, non-dimensional duration fact (else NULL with a `warehouse:fiscalYearUnknown` warning) |

Discovery stores one `companies` row per LEI (`cik` = entity key, `name`; the other
columns stay NULL). Its facts carry the report's own entity scheme and identifier
(the LEI scheme and LEI).

- **Package.** The bronze package archive is the report package zip as published,
  byte-identical, at `bronze/esef/filings/<entity key>/<filing key>/packages/sha256=<published
  sha256>/package.zip`. Its bytes must match filings.xbrl.org's `sha256`; a mismatch
  stores nothing. A package that matches but fails the ZIP checks (traversal,
  member count, sizes, ratio) or has no single inline report under `reports/` is kept
  at `quarantine/esef/filings/<filing key>/sha256=<sha256>/package.zip` and the fetch
  fails permanently.
- **Manifest.** Members are the zip entries (path inside the zip, size, SHA-256);
  exactly one, the inline XBRL report under `<top>/reports/`, is the entrypoint
  (`report_url` decides between several). It is stored at
  `manifests/esef/filings/<filing key>/package-sha256=<sha256>/filing-sha256=<hash of
  the FilingRef>.json`; a second fetch of the same bytes reuses it without
  downloading the package again.
- **Parse.** Arelle reads the report from the extracted package; references may reach
  anything under the package's top directory, and the package's
  `META-INF/catalog.xml` remaps its own extension taxonomy URLs to package files.
  A catalog entry that leaves the package or overlaps a pinned taxonomy prefix fails
  the extraction.

## EDINET filings

Japanese securities reports come from the FSA's EDINET API v2 (`sources/edinet.py`,
source code `edinet`). Discovery reads the document list
(`/api/v2/documents.json?date=<day>&type=2`) for each day of the window and keeps
records with `xbrlFlag` `1`, `withdrawalStatus` `0`, `disclosureStatus` not `1`/`2`
(hidden by the FSA), not a metadata-correction notice (`docInfoEditStatus` `1`), a
`docTypeCode` in the run's forms (default `XBRL_EDINET_DOC_TYPES`), and the requested
EDINET code when one is given. Each kept record becomes:

| Field | Value |
|---|---|
| `accession` (filing key) | `edinet-<docID>`, e.g. `edinet-S100TR7I`; a record with a malformed `docID` or `edinetCode` is skipped and logged |
| `cik` (entity key) | `resolve_entity_key("http://disclosure.edinet-fsa.go.jp", <edinetCode>)` (upper-cased) |
| `company_name` | `filerName` (the EDINET code if empty) |
| `form` | `docTypeCode`: 120 annual securities report, 130 its amendment, 140/150 quarterly, 160/170 semi-annual |
| `filed_date` | the date of `submitDateTime` (Japan time) |
| `report_date` | `periodEnd` |
| `is_amendment` | true for 130, 150, 170 |
| `archive_directory_url` | `https://api.edinet-fsa.go.jp/api/v2/documents/<docID>` (no key) |
| `is_inline_xbrl` | false (the entrypoint is the `.xbrl` instance) |
| `fiscal_year` | the year of `jpdei_cor:CurrentFiscalYearEndDateDEI`; without it, `report_date`'s year for 120/130, else unchanged |
| `fiscal_period` | `jpdei_cor:TypeOfCurrentPeriodDEI` (`FY`, `Q1`, `Q2`, `Q3`, `HY` as `H1`); without it, `FY` for 120/130, else unchanged |

Only non-nil, non-dimensional jpdei facts count; conflicting or invalid values leave
the field to the fallback with a `warehouse:fiscalFocusInvalid` warning.

- **Package.** The `type=1` zip ("submitted document and audit report") stored
  byte-identical at `bronze/edinet/filings/<entity key>/<filing key>/packages/sha256=<sha256>/package.zip`.
  EDINET answers errors with HTTP 200 and a JSON body; a non-zip body is a
  `SourceAccessError` (`SourceRateLimited` for status 429) and stores nothing.
- **Manifest.** Members are the zip entries; exactly one, the instance directly under
  `XBRL/PublicDoc/` (`*.xbrl`), is the entrypoint. The audit report's instance under
  `XBRL/AuditDoc/` is a plain member. No instance or several, or a zip that fails the
  archive checks, quarantines the zip at
  `quarantine/edinet/filings/<filing key>/sha256=<sha256>/package.zip`. Member
  `source_url` is the keyless download URL (`...?type=1`).

## DART filings

Korean periodic reports come from the FSS's OpenDART API (`sources/dart.py`, source
code `dart`). Discovery pages `/api/list.json` (`bgn_de`/`end_de` from the window,
`pblntf_ty=A`, `page_count` = `XBRL_DART_PAGE_COUNT`, `corp_code` for one company)
through `total_page`. Without `corp_code` OpenDART searches at most three months, so
a longer window becomes consecutive searches, each from its start date to the day
before the same day three calendar months later (a day the month lacks becomes its
last day) or `filed_to`, in order (`search_windows`); with `corp_code` the window is
one search. Status `013` (no data) is an empty search. Report names map to
report codes:

| `report_nm` | `form` (reprt_code) | `fiscal_period` |
|---|---|---|
| `사업보고서 (YYYY.MM)` (business report) | `11011` | `FY` |
| `반기보고서 (YYYY.MM)` (half-year report) | `11012` | `H1` |
| `분기보고서 (YYYY.03)` (quarterly report) | `11013` | `Q1` |
| `분기보고서 (YYYY.09)` | `11014` | `Q3` |

Other reports, and quarterly reports for other months (a non-December year end's
quarter can't be told apart without the year end), are skipped. A leading tag with
`정정` (`[기재정정]`, `[첨부정정]`) sets `is_amendment`; a remark (`rm`) containing `철`
(withdrawn) skips the report.

| Field | Value |
|---|---|
| `accession` (filing key) | `dart-<rcept_no>`, e.g. `dart-20240312000736` |
| `cik` (entity key) | `resolve_entity_key("https://opendart.fss.or.kr/corp_code", <corp_code>)` |
| `company_name` | `corp_name` |
| `filed_date` | `rcept_dt` |
| `report_date` | the last day of the `(YYYY.MM)` month |
| `archive_directory_url` | `https://opendart.fss.or.kr/api/fnlttXbrl.xml?rcept_no=<rcept_no>&reprt_code=<code>` (no key) |
| `fiscal_year` | `report_date`'s year |

- **Package.** The original XBRL zip from `/api/fnlttXbrl.xml` (`rcept_no`,
  `reprt_code`), stored byte-identical under `bronze/dart/...`. A non-zip body is
  OpenDART's status message: `020` (daily limit) raises `SourceRateLimited`, anything
  else (`013`, `014`, key errors) `SourceAccessError`; nothing is stored.
- **Manifest.** Exactly one `.xbrl` member is the entrypoint; none or several, or a
  zip that fails the archive checks, quarantines the zip under `quarantine/dart/`.

Both sources store one `companies` row per entity key (`cik`, `name`) from
discovery. API keys are never part of a filing reference, manifest, object key or
metadata, job payload, or log record (SECURITY.md).

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

## Extraction records

`xbrl_extractions` has one row per published `(accession, extraction_revision)`, written
last: `entity_counts_json` and `metadata_json` (the bundle's document metadata). The
Arelle validation log (about 1 MB per filing) isn't copied into `metadata_json`; its
provenance keeps `validation_log_records`, the record count. The full log stays in the
saved extraction, and its issues are grouped in `validation_issues`. Rows written
before this change still embed the log.

`storage retire-revisions` can delete the `xbrl_*` and `canonical_facts` rows of
revisions older than the published one (OPERATIONS.md); their `xbrl_extractions` row,
silver Parquet, and saved extraction stay.

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
  Several roles of one kind are all kept. A combined "Statements of Operations and
  Comprehensive Income" (operations/income/earnings named outside "comprehensive
  income", plus income-statement anchors such as `Revenues`, `NetIncomeLoss`, or EPS)
  is an `income_statement`; a comprehensive-income-only title stays
  `comprehensive_income`.
- **Lines.** One row per line: a pre-order walk of the role's parent-child arcs by
  `order` (`line_number` from 1 per role, `depth` 0 at the root). `label` uses the
  arc's preferred label role, else the standard label; `is_total` marks the total
  label role. A concept presented on two statements appears on both. Rows have no
  inherent order: always `ORDER BY role_uri, line_number`.
- **Line roles.** `line_role` marks hypercube scaffolding: `table`, `axis`, `domain`,
  `member` (from the concept's substitution group and type, else its name), or
  `concept` for reported line items.
- **Values.** Parallel arrays `value_fact_ids`, `value_period_starts`,
  `value_period_ends`, `value_period_instants`, `value_decimals`: one entry per
  period, from the concept's non-dimensional, first-ordinal facts, latest period
  first. Lines without facts have empty arrays. `value_concept_qnames` names each
  value's concept. A `member` line (for example Products or Services under the
  product/service axis) gets the facts of the role's line item concepts whose only
  dimension is that member on the nearest enclosing axis, one per concept and period.
  Other dimensional cells (equity components, multi-axis tables) are not populated.
- `v_statement_lines_latest` shows only each accession's latest published revision.

```sql
SELECT line_number, depth, label, is_total, value_period_instants, value_decimals
FROM xbrl.v_statement_lines_latest
WHERE accession = '0000320193-25-000079' AND statement_kind = 'balance_sheet'
ORDER BY role_uri, line_number;
```

### Cover pages, breakdowns, and text blocks

Derived by `normalization/disclosures.py` from the extraction's facts and written with
the bundle (silver checkpoints `cover_pages` and `segment_facts`, same revision
marker). Raw facts are unchanged; every row is rebuildable from silver. Filings
completed before these entities existed have no rows until they are re-extracted.

- **`xbrl_cover_pages` / `v_cover_pages_latest`.** One row per filing:
  `document_type`, `period_end_date`, `registrant_name`, `filer_category`,
  `amendment`, `trading_symbol`, `exchange`, `auditor_name`, `shares_outstanding`,
  `shares_outstanding_date`, `public_float`. Only non-dimensional dei facts count; with
  several values the first in document order wins; a missing fact gives NULL. Shares
  outstanding sums one value per `StatementClassOfStockAxis` member at the latest
  instant when no non-dimensional value exists. Facts on any other axis, including
  co-registrants on `dei:LegalEntityAxis`, are ignored. The exception is
  per-security data: `TradingSymbol`, `SecurityExchangeName`, and `Security12bTitle`
  on the class-of-stock axis fill the parallel arrays `security_members`,
  `security_titles`, `security_symbols`, `security_exchanges` (document order).
  Without a non-dimensional ticker or exchange, both come from one security: the one
  matching a non-dimensional ticker, else common stock (`CommonStockMember` or a title
  containing "Common Stock"), else the first listed.
- **`xbrl_segment_facts` / `v_segment_facts_latest`.** One row per numeric,
  first-ordinal fact whose dimensions are **all** on these axes: `us-gaap:
  StatementBusinessSegmentsAxis` (`segment`), `srt:ProductOrServiceAxis` (`product`),
  `srt:StatementGeographicalAxis` (`geography`), `srt:ConsolidationItemsAxis`
  (`consolidation`). Consolidated facts and facts with any other axis (for example
  `RestatementAxis`) are excluded. `axis`/`member_label` describe the first member in
  that order; `member_axes`, `member_qnames`, `member_labels` list every member. Labels
  are the member's standard label, else its local name without `Member`
  (`USMember` → `US`); a member without a concept in the bundle has a Clark-notation
  `member_qname` unless its namespace is a standard family. `is_elimination` marks
  `…Elimination(s)Member` members (for example `srt:IntersegmentEliminationMember`).
  Recast segments stay visible: each filing keeps its own rows.
- **`v_text_blocks_latest`.** Every `…TextBlock` fact of the latest revision from
  `v_xbrl_facts_latest`, HTML as filed in `value_text`, with `is_policy` for
  `…PolicyTextBlock` concepts.
- The `_latest` views filter by each accession's latest published revision only,
  never across filings.

```sql
-- Apple FY2025 revenue by segment
SELECT member_label, value
FROM xbrl.v_segment_facts_latest
WHERE accession = '0000320193-25-000079' AND axis = 'segment' AND length(member_axes) = 1
  AND concept_qname = 'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'
  AND period_end = '2025-09-27' AND period_start = '2024-09-29';

-- Cover page and accounting policies of one filing
SELECT document_type, registrant_name, shares_outstanding, shares_outstanding_date
FROM xbrl.v_cover_pages_latest WHERE accession = '0000320193-25-000079';
SELECT concept_name, value_text
FROM xbrl.v_text_blocks_latest WHERE accession = '0000320193-25-000079' AND is_policy;
```

The query API serves the same views per filing (bearer token when `XBRL_API_KEY` is
set):

```bash
# Cover page; `securities` lists {member, title, symbol, exchange}. 404 without a row.
curl -H "Authorization: Bearer $XBRL_API_KEY" \
  http://127.0.0.1:8080/v1/filings/0000320193-25-000079/cover-page
# Segment revenue. `kind` repeats (segment, product, geography, consolidation) and
# matches any member; include_eliminations=false drops elimination rows;
# limit 1-10,000 (default 1,000), offset 0-10,000.
curl -H "Authorization: Bearer $XBRL_API_KEY" \
  "http://127.0.0.1:8080/v1/filings/0000320193-25-000079/segments?kind=segment&concept=us-gaap:Revenues"
# Accounting policies with their HTML. policy=false lists the other text blocks;
# limit 1-1,000 (default 200), at most 100 with include_text=true (default 100).
curl -H "Authorization: Bearer $XBRL_API_KEY" \
  "http://127.0.0.1:8080/v1/filings/0000320193-25-000079/text-blocks?policy=true&include_text=true"
```

List responses are `{accession, items, count, limit, offset}`. Segment items carry
`kind` (the stored `axis`) and `members` (`[{kind, qname, label}]`); text-block items
carry `text_length` in characters and `text` only on request. Decimals are JSON
numbers: exact when integral, otherwise the nearest double.

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

One row per raw fact, metric, and extraction revision. The sort key leaves out the
mapping, so after `mapping remap` the new mapping's row replaces the older one on
merge (`ReplacingMergeTree(mapping_version)` keeps the highest version), the same row
readers pick with `ORDER BY mapping_version DESC LIMIT 1 BY`. Mapping history stays
in PostgreSQL `concept_mappings`.

Contains:

- raw fact ID;
- canonical metric code;
- normalized decimal/text value;
- normalized unit and sign;
- mapping ID/version/confidence/explanation;
- dimensional scope;
- source and extraction revision.

### Acceptance time and filing context

`xbrl_facts.accepted_at` and `canonical_facts.accepted_at` hold the SEC acceptance
time from the filing reference (`FilingRef.accepted_at`, from the submissions JSON).
`canonical_facts` also carries the filing's dei `fiscal_year`, `fiscal_period`, and
`report_date`, and the raw fact's `concept_namespace`/`concept_qname`. These columns
are filled by re-extraction at revision 3 (no backfill of older rows); existing tables
gain them as NULL through `xbrl-warehouse db upgrade-clickhouse --yes`. Daily-index
discovery doesn't supply acceptance times, so series fall back to midnight UTC of the
filing date for those filings.

### `metric_series`

Standardized series rebuilt per company from `canonical_facts` by
`normalization/series.py` and `query/series.py` (derived; fully rebuildable).

- **Inputs.** The company's consolidated canonical facts (`dimensions_json = '[]'`)
  from each filing's latest published extraction revision.
- **Selection per (metric, period), before any derivation.** Within one filing the
  lowest seed `priority` wins (`config/concept_seed.yml`; unseeded concepts rank last);
  across filings `latest` takes the newest acceptance and `as_reported` the oldest; an
  as-of cutoff drops observations accepted after it first.
- **Periods.** Durations of 80-100 days are quarters, 170-190 six months, 260-285 nine
  months, 350-380 a year (52/53-week calendars fit). Discrete quarters are reported
  3-month values, else differences of consecutive year-to-date values sharing a
  `period_start` (`ytd_difference`); Q4 = year - nine months
  (`annual_minus_nine_months`). Only when the year has no nine-month value, Q4 =
  year - (Q1 + Q2 + Q3) (`annual_minus_quarters`): Q1 starts on the year's start, Q1-Q3
  are contiguous discrete quarters, and Q4 starts the day after Q3 ends.
  TTM (`ttm_sum`) sums four contiguous discrete quarters.
  Instants are `instant` points; there is no TTM for them. A derived point needs every
  input and one unit.
- **Labels.** `fiscal_year`/`fiscal_quarter` come from the filing whose `report_date`
  equals the point's period end (FY maps to quarter 4).
- **Formulas.** Formula metrics from `canonical_metrics.yml` (`free_cash_flow`) are
  computed per period when every operand exists (`formula`).
- **Lineage.** Each point records `concept_qname`, `source_accessions`, and
  `available_at` (the newest acceptance among its inputs).
- **Storage.** `as_reported` rows once, with `known_from = available_at`. `latest`
  rows are versions: the series is rebuilt as of each filing's acceptance time and a
  row is written whenever a point changes (`known_from` = that time). A query takes the
  newest `built_at` for the company and, per point, the newest version with
  `known_from <= as_of`.
- **Rebuilds.** A completed `parse_filing` (or legacy `ingest_filing`) or
  `map_canonical` queues the company's `rebuild_series` job, which runs after
  `XBRL_SERIES_REBUILD_DELAY_SECONDS`; requests in the meantime share one rebuild, and
  a failed rebuild retries like any job (OPERATIONS.md).
  `xbrl-warehouse series rebuild --cik <cik>` rebuilds on demand. The table is a
  `ReplacingMergeTree(built_at)`, so merges replace older builds' rows without a delete
  mutation per rebuild; readers only use the newest build.
- **Non-additive metrics.** Metrics with `additive: false` in `canonical_metrics.yml`
  (weighted-average shares, shares outstanding, basic and diluted EPS) keep their
  reported quarters, years, and instants only: no year-to-date quarters, Q4 = FY - 9M,
  or TTM, because differences and sums of averages or per-share values are
  meaningless. `dividends_per_share` stays additive (per-share dividends do sum). The
  flag is read from the YAML; it isn't stored in PostgreSQL `canonical_metrics`.

`companies` in ClickHouse mirrors PostgreSQL company rows (name, tickers, exchanges,
SIC, fiscal year end) for read-only ticker lookups by the API; the newest mirrored
holder of a ticker wins.

## Row hashing

Every silver/raw warehouse row should have a deterministic hash over normalized field serialization. Hashing excludes ingest timestamps but includes all source-controlled values. This supports replay verification and source-revision comparison.
