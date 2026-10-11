# 19 — EU/UK ESEF filings through filings.xbrl.org

Status: draft for review (11 October 2026). Depends on plans 17 (filing and entity
keys) and 18 (source adapters). First real non-SEC source.

## Problem

EU-listed issuers file annual reports in ESEF (inline XBRL, IFRS taxonomy, LEI as the
entity identifier); UK issuers file the same format. XBRL International's
filings.xbrl.org collects them and serves a free JSON:API at
`https://filings.xbrl.org/api`. After plan 18 the warehouse can host a second source,
but none exists.

Verified on 11 October 2026 from a live `GET /api/filings?include=entity`:
filing attributes are `fxo_id`, `country`, `date_added`, `processed`, `period_end`,
`sha256`, `package_url`, `report_url`, `json_url`, `viewer_url`, `error_count`,
`warning_count`, `inconsistency_count`; URLs are host-relative; the included entity
has `identifier` (the LEI) and `name`. Pagination is `page[size]`/`page[number]` with
`links.next`; `sort=-date_added` and `filter[<attribute>]=` work.

Terms: the API is free, "may be changed at any time", may be rate limited or
withdrawn, and the site states no data licence. **Before enabling `esef` in
production, the operator confirms reuse terms with XBRL International.**

### Before / after

| | Before | After |
|---|---|---|
| `discover_filings {"source": "esef", "filed_from": "2024-04-01", "filed_to": "2024-04-30"}` | `UnsupportedSourceError` | fetch jobs for each ESEF report added in that window |
| Kainos Group 2023 annual report | not reachable | `esef-213800H2PQMIF3OVZY47-2023-12-31-ESEF-GB-0`, entity key from its LEI |
| Its revenue | — | `ifrs-full:Revenue` facts in `v_xbrl_facts_latest`, mapped by the existing `ifrs-full` seeds |

## Scope

Contract surface:

- `sources/esef.py`: `ESEF_POLICY` (`code="esef"`, hosts `{"filings.xbrl.org"}`, at
  most 2 requests/s, the warehouse user agent), `LEI_SCHEME =
  "http://standards.iso.org/iso/17442"`, and `EsefSourceAdapter(settings, http,
  object_store, entity_keys)` where `entity_keys` has `async
  resolve_entity_key(scheme, identifier)` (the control repository in production).
- Settings: `esef_page_size` (default 100), `esef_max_pages` (default 50),
  `esef_countries` (default empty: all).
- **discover**: `GET /api/filings` with `sort=-date_added`, `include=entity`,
  `page[size]`, `page[number]` (and `filter[country]` per configured country). Keep
  records whose `date_added` date is inside `[filed_from, filed_to]` (open bounds
  allowed) and, when `request.entity` is set, whose LEI equals it. Stop when a page's
  oldest `date_added` is before `filed_from`, when there's no `links.next`, or at
  `esef_max_pages`. Each kept record becomes a `FilingRef`:
  `accession = "esef-" + fxo_id`, `cik = resolve_entity_key(LEI_SCHEME, LEI)`,
  `company_name` = entity name, `form = "ESEF"`, `filed_date` = `date_added` date,
  `report_date` = `period_end`, `archive_directory_url` = `https://filings.xbrl.org` +
  the package URL's directory, `primary_document_url` from `report_url`,
  `is_inline_xbrl = True`. A record whose key isn't a valid filing key is skipped and
  logged. One `Company` per LEI (`cik` = entity key, `name`).
- **download_package**: re-read the record (`filter[fxo_id]`), download
  `package_url`, check the bytes against `sha256` (`ArtifactIntegrityError` on
  mismatch, nothing stored), validate the archive with the existing zip limits
  (`filing_max_members`, `filing_max_bytes`, ratios, no absolute or `..` names), store
  it once at `filing_package_key(filing, sha256)`, and return a manifest whose members
  are the archive entries, with exactly one `is_xbrl_entrypoint` member: the inline
  report under `reports/` (`.xhtml`/`.html`). No report → `ArtifactIntegrityError`.
  Re-downloading identical bytes is idempotent.
- **fiscal_focus**: `fiscal_period = "FY"`; `fiscal_year` = `report_date.year`, or when
  absent the end year of the latest non-dimensional duration fact.
- **parse**: `parse_filing` opens the stored package and extracts from the entrypoint
  member (Arelle opens report packages). Changes to the parse path, if needed, stay
  source-neutral.
- **Taxonomies (AC-5)**: `config/taxonomy_packages.yml` gains pinned entries for the
  ESMA ESEF taxonomy packages and the IFRS Accounting Taxonomy years in use, and
  `source_required_prefixes.esef`. The taxonomy host allowlist adds
  `www.esma.europa.eu`, `xbrl.ifrs.org`, `www.ifrs.org` (SECURITY.md). Computing the
  SHA-256 pins needs one download per package: an operator step with network
  approval (see Verification).
- Docs: DATA_MODEL (ESEF keys and fields), OPERATIONS (enable `esef`, run one esef
  fetch worker, terms caveat), SECURITY (new hosts, third-party aggregator trust),
  ARCHITECTURE (deployment and component views), `.env.example`.

Exclusions: ESEF-specific validation rules (ESMA filing rules), UK FCA's own feed,
national OAMs, translation of labels, and anything beyond annual reports.

Invariants: SEC behavior unchanged; ESEF traffic only to filings.xbrl.org (and the
taxonomy hosts during `taxonomy sync`); raw packages stored byte-identical;
filings.xbrl.org's published digest is checked, never trusted blindly.

![ESEF adapter flow: filings.xbrl.org JSON:API into the adapter's discovery, download and fiscal-focus steps, the plan 18 registry and worker, plan 17 object keys, pinned ESEF and IFRS taxonomies, and offline Arelle parsing, linked to AC-1 to AC-6](diagrams/esef-source.svg)

## Acceptance criteria

- **AC-1 Discovery.** Window, LEI filter, newest-first paging with stop rules and page
  cap, record mapping, one entity key and company per LEI, unsafe records skipped.
- **AC-2 Package.** Digest check, archive limits and traversal rejection, single
  report entrypoint, idempotent storage at the plan 17 key.
- **AC-3 Fiscal focus.** `FY` and the period-end year, with the fact fallback.
- **AC-4 Policy.** Only `filings.xbrl.org`, ≤ 2 requests/s, lock `xbrl:esef-egress`.
- **AC-5 Taxonomies (manual + review).** Pinned ESEF/IFRS entries and
  `source_required_prefixes.esef`; `taxonomy status` shows no `uncovered` rows with
  `esef` enabled.
- **AC-6 Canary (manual, operator).** With approval: enable `esef`, discover one week,
  ingest three filings offline, confirm `ifrs-full` facts and canonical revenue.
- **AC-7 Regression.** Validate gates.

| Criterion | Acceptance | Verification |
|---|---|---|
| AC-1 | window, stop, mapping, LEI filter | page cap, unsafe record, one key per LEI |
| AC-2 | stored archive, entrypoint, digest mismatch | traversal, non-zip, member limit, no report, idempotent |
| AC-3 | FY from report date | FY from facts |
| AC-4 | policy and lock | (host checks in AC-2) |
| AC-5–AC-6 | manual | manual |

## Implementation sequence

1. Policy, settings, adapter discovery (AC-1, AC-4).
2. Package download and manifest (AC-2); parse-path check with a stored package.
3. Fiscal focus (AC-3). Registry wiring for `esef` in `runtime_services`.
4. Taxonomy entries and hosts (AC-5, with the operator's pins). Docs. Validate.

## Verification

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/19-esef-source/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/19-esef-source/verification
```

No live calls: the tests serve canned JSON:API pages and zip packages through
`httpx.MockTransport`. The verification set imports `acceptance/fxo_fixtures.py`.

Operator steps (each needs explicit approval because they send non-SEC traffic):

1. Confirm filings.xbrl.org reuse terms.
2. Download the ESEF and IFRS taxonomy packages once, record their SHA-256 in
   `taxonomy_packages.yml`, run `taxonomy sync` and `taxonomy status`.
3. AC-6 canary.

### Baseline (plan 17 candidate 131a87f, 11 October 2026)

All tests fail at import: `sec_xbrl_warehouse.sources` (plan 18) and
`sources.esef` don't exist yet. Rerun the baseline on the accepted plan 18 commit
before handing this plan to an implementer.

## Completion evidence

### Implementer record

### Verifier record
