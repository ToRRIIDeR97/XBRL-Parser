# 21 — Operator file inbox, with India (NSE and MCA) profiles

Status: draft for review (11 October 2026). Depends on plans 17 and 18. Plan 22
reuses this adapter for Singapore.

## Problem

India has machine-readable XBRL, but no free regulator API comparable to EDGAR,
EDINET or DART:

- **NSE / BSE.** Listed companies file financial results in XBRL; NSE's corporate
  filings pages link an "XBRL file" per submission ("Integrated Filing – Financials"
  from the March 2025 quarter on). No public bulk API or data licence was found
  (11 October 2026); the exchange sites use bot protection, and their terms weren't
  confirmed.
- **MCA21.** Companies file Ind AS financial statements (AOC-4 XBRL) with the
  Ministry of Corporate Affairs; public copies are sold per document.

Automated crawling of these sites isn't acceptable without confirmed terms, so this
plan doesn't fetch anything. It adds an **inbox adapter**: the operator obtains files
under the source's terms (manual download, purchase, a licensed vendor) and places
them with an `index.csv` in `data/inbox/<source>/`. From there they go through the same
discovery → fetch → parse jobs as every other source, with the same raw-artifact
guarantees.

### Before / after

| | Before | After |
|---|---|---|
| Reliance Q4 FY2024 results XBRL downloaded by the operator | can't be loaded | `index.csv` row → `nse-RELIANCE-2024-Q4`, entity key from ISIN `INE002A01018` |
| An AOC-4 XBRL bought from MCA | can't be loaded | `mca-<id>` filing, entity key from the CIN |
| A typo in the index | n/a | `xbrl-warehouse inbox check --source nse` lists it before anything is queued |

## Scope

Contract surface:

- Settings: `inbox_dir: Path = Path("data/inbox")` (`XBRL_INBOX_DIR`), created by
  `ensure_directories`.
- `sources/inbox.py`:
  - `InboxProfile(code, entity_scheme, entity_pattern, default_fiscal_period)`.
  - `NSE_PROFILE`: `code="nse"`, `entity_scheme="urn:isin"`, ISIN pattern
    `^[A-Z]{2}[A-Z0-9]{9}[0-9]$`, default `FY`.
  - `MCA_PROFILE`: `code="mca"`, `entity_scheme="https://www.mca.gov.in/cin"`, CIN
    pattern `^[LU][0-9]{5}[A-Z]{2}[0-9]{4}[A-Z]{3}[0-9]{6}$`, default `FY`.
  - `InboxSourceAdapter(settings, object_store, entity_keys, profile)`; its `code` is
    the profile's.
- **Index** `<inbox_dir>/<code>/index.csv`, UTF-8 with a header. Required columns:
  `native_id, entity_id, company_name, form, filed_date, period_end, file`; optional:
  `entity_scheme` (overrides the profile's), `fiscal_period`, `sha256`, `source_url`
  (provenance only, never fetched). A missing required column → `ValueError` naming it.
  Duplicate `native_id` → `ValueError` naming it. No folder or no rows → empty result.
- **discover**: each valid row becomes `FilingRef(accession = f"{code}-{native_id}",
  cik = resolve_entity_key(scheme, entity_id), form, filed_date, report_date =
  period_end, fiscal_period = row value or profile default, fiscal_year = period_end
  year, archive_directory_url = "inbox://<code>/<native_id>")` filtered by window
  (`filed_date`), `request.entity` and `request.forms`. Rows with an invalid key, ISO
  date or entity id (profile pattern) are skipped and logged. One `Company` per entity.
- **download_package**: resolve `file` inside `<inbox_dir>/<code>/` after following
  symlinks (absolute paths, `..` and escaping links → `ArtifactIntegrityError`), require
  it to exist and fit `filing_max_bytes`, check `sha256` when given. A `.zip` is
  validated with the existing zip limits and must contain exactly one instance
  (`.xbrl`, `.xml` with an XBRL root, `.xhtml`/`.html` inline); a single `.xbrl`/`.xml`/
  `.xhtml`/`.html` file is stored as a one-member package. Anything else →
  `ArtifactIntegrityError`. The package is stored at `filing_package_key`; the inbox
  file is never modified or deleted. Re-reading changed bytes yields a new
  content-addressed package (the earlier one stays).
- **fiscal_focus**: keeps the discovery values; fills `FY` and the `report_date` year
  when absent.
- No HTTP client and no egress lock for inbox sources; `runtime_services` builds an
  inbox adapter for each enabled inbox profile.
- **CLI** `xbrl-warehouse inbox check --source <code>`: validates every row (fields,
  key, entity id, file containment and existence, digest when given), prints one line
  per problem with the row's `native_id` or file, then `<n> valid, <m> problems`;
  exit code 1 when there are problems. Queues nothing.
- **Taxonomies (AC-6)**: pins for the NSE/BSE results taxonomies and the MCA Ind AS
  taxonomy (operator downloads with approval), `source_required_prefixes.nse/mca`.
- Docs: OPERATIONS (inbox layout, index columns, `inbox check`, the terms rule),
  DATA_MODEL (keys, `inbox://` provenance), SECURITY (path containment, operator-supplied
  files are untrusted input), `.env.example`.

Exclusions: any crawler or scraper for NSE, BSE or MCA; BSE as a separate profile
(its files can use `nse`'s ISIN profile or a later profile); PDF results.

Invariants: no network access from inbox sources; inbox files never leave
`inbox_dir/<code>`; raw bytes stored unchanged; SEC behavior unchanged.

![Operator-supplied files and index flowing into the inbox adapter with NSE and MCA profiles, then the plan 18 registry and worker, plan 17 storage, and operator-pinned Indian taxonomies, linked to AC-1 to AC-6](diagrams/file-inbox.svg)

## Acceptance criteria

- **AC-1 Index → filings.** Mapping, window/entity/form filters, fiscal fields,
  duplicates, missing columns, empty states, bad rows skipped.
- **AC-2 Packages.** Single file and zip packages, entrypoint, storage key, inbox
  unchanged, content addressing on change.
- **AC-3 Containment and limits.** `..`, absolute paths, escaping symlinks, digest
  mismatch, size, missing file, two instances, non-XBRL file.
- **AC-4 Profiles.** Codes and schemes; no network.
- **AC-5 `inbox check`.** Problems listed, summary line, exit code, nothing queued.
- **AC-6 Taxonomies (manual + review).**
- **AC-7 Canary (manual, operator).** Three operator-supplied NSE results files load
  and parse offline.
- **AC-8 Regression.** Validate gates.

| Criterion | Acceptance | Verification |
|---|---|---|
| AC-1 | mapping, window, fiscal fields | duplicates, missing column, empty, filters, bad rows, defaults |
| AC-2 | single file, zip | changed bytes |
| AC-3 | `..`, absolute, digest | symlink escape, size, two instances, text file, missing file |
| AC-4 | profiles | — |
| AC-5 | CLI | — |
| AC-6–AC-7 | manual | manual |

## Implementation sequence

1. Settings, profiles, index reader (AC-1, AC-4).
2. Package building with containment (AC-2, AC-3).
3. `inbox check` (AC-5); registry wiring for enabled inbox profiles.
4. Taxonomy entries (AC-6, operator pins). Docs. Validate.

## Verification

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/21-file-inbox-india/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/21-file-inbox-india/verification
```

Local files only. The verification set imports `acceptance/inbox_fixtures.py`.

### Baseline (plan 17 candidate 131a87f, 11 October 2026)

All tests fail at import (`sec_xbrl_warehouse.sources` from plan 18 is missing; no
`inbox` CLI group). Rerun on the accepted plan 18 commit before implementation.

## Completion evidence

### Implementer record

Branch `plan/21-file-inbox-india`, from plan 20's accepted commit 0cebc06 (13 October
2026). Only `implementation.md`, `diagrams/` and `acceptance/` were copied; the
`verification/` folder wasn't read, copied, or run. The copied `acceptance/` and
`implementation.md` match the main checkout's (`diff -r`, `cmp`). Nothing sent a
request anywhere: no code path or test touches NSE, BSE, MCA, the SEC, or a taxonomy
host. The main checkout's `.venv` and `.env` are symlinked and not committed. No
container was restarted and alembic wasn't run.

**Baseline (unmodified 0cebc06).** Acceptance: 8 failed, 0 passed: 7 import errors
(`sec_xbrl_warehouse.sources.inbox` missing) and 1 usage error (no `inbox` CLI group,
exit 2).

**Criteria.**

- AC-1: `sources/inbox.read_index` reads `<inbox_dir>/<code>/index.csv` (UTF-8, BOM
  allowed, header names trimmed). A missing required column raises `ValueError` naming
  the first one missing; a repeated `native_id` raises `ValueError` naming it; no folder,
  no index, or a header-only index gives an empty result. Each row is validated into an
  `InboxRow` with its problems: filing key (`is_filing_key("<code>-<native_id>")`),
  ISO dates (`YYYY-MM-DD` only), the profile's entity pattern, a fiscal period from
  `FISCAL_PERIODS`, an entity scheme other than the SEC CIK scheme, a non-empty form,
  an `http(s)` `source_url` without credentials, and no cells beyond the header.
  `InboxSourceAdapter.discover` skips rows with problems (logged as
  `inbox_row_skipped`), filters by `filed_date` window, `entity` (the row's
  `entity_id`) and `forms`, and builds the `FilingRef` as the contract says:
  `accession=<code>-<native_id>`, `cik=resolve_entity_key(scheme, entity_id)` (once per
  identity per run), `report_date=period_end`, `fiscal_year=period_end.year`,
  `fiscal_period` from the row or the profile default, and
  `archive_directory_url=inbox://<code>/<native_id>`. One `Company` per entity key.
- AC-2: `download_package` reads the row again, copies the file into a temporary
  directory, checks the digest, and stores it through plan 20's
  `sources/keyed.store_zip_package` (bulk zip checks scoped to the filing limits, safe
  member names, byte-identical storage at `filing_package_key`, manifest cache keyed by
  package SHA-256 and the `FilingRef`). A `.zip` keeps its members; a single
  `.xbrl`/`.xml`/`.xhtml`/`.html` file becomes a one-member zip written with the
  existing `_write_member` (fixed timestamps), so the same bytes give the same package
  and a replay reuses it. Changed bytes give a new package key and the earlier object
  stays. The instance is exactly one member: `.xbrl`, an `.xml` whose root element is
  `{http://www.xbrl.org/2003/instance}xbrl`, or an `.xhtml`/`.html` declaring the
  inline XBRL namespace (lxml `iterparse` without DTDs, entities, or network). The
  inbox file is never written.
- AC-3: `contained_path` refuses empty, absolute (POSIX or Windows drive), backslash
  and `..` paths, and anything whose fully resolved path (every symlink followed) is
  outside the resolved `<inbox_dir>/<code>` or is that folder itself. The copy opens
  the resolved path with `O_NOFOLLOW` where available, requires a regular file, and
  stops past `filing_max_bytes`. Missing files, size, digest mismatch (and a malformed
  `sha256`), and a suffix other than `.zip` or an instance suffix raise
  `ArtifactIntegrityError` and store nothing. A package with no instance or several,
  or a zip that fails the archive checks, raises `ArtifactIntegrityError` and is kept
  under `quarantine/<code>/filings/<key>/sha256=<digest>/package.zip` (plan 19/20
  convention); nothing goes under `bronze/` or `manifests/`.
- AC-4: `NSE_PROFILE` (`nse`, `urn:isin`, ISIN pattern, `FY`) and `MCA_PROFILE`
  (`mca`, `https://www.mca.gov.in/cin`, CIN pattern, `FY`), collected in
  `INBOX_PROFILES`. The adapter has no HTTP client; `source_policies` is unchanged (no
  policy, so no host and no lock for inbox codes). `runtime_services` accepts enabled
  inbox codes as implemented and adds an `InboxSourceAdapter(settings, store, control,
  profile)` for each, building no client or lock even for fetch workers.
- AC-5: `xbrl-warehouse inbox check --source <code>` reads the index (duplicates
  allowed so it can list them), validates every row plus `file_problems`
  (containment, existence, size, digest when given), prints one line per problem as
  `<native_id or "line N"> (file <file>): <problem>`, then `<n> valid, <m> problems`,
  and exits 1 when there are problems. A missing column is one problem line. Unknown
  columns print as warnings (not counted). An unknown source code is a usage error
  (exit 2). It builds no control repository and queues nothing.
- AC-6 (partial, see operator steps): `source_required_prefixes.nse =
  [www.bseindia.com/xbrl/, www.nseindia.com/xbrl/]` and `.mca =
  [www.mca.gov.in/XBRL/]`, equal to the profiles' `taxonomy_prefixes` (a test checks
  both). They're unverified and have no pinned entries, so with either source enabled
  `taxonomy status` and `doctor` fail and offline extraction refuses to start (fail
  closed). No host was added to the taxonomy allowlist.
- AC-7: manual (operator canary), not run.
- AC-8: Validate gates below.
- Settings: `inbox_dir` (`XBRL_INBOX_DIR`, default `data/inbox`), created by
  `ensure_directories`.

**Regression tests added:** `tests/unit/test_inbox_source.py`, 26 tests: missing
column and duplicate refusal; empty states; filters, defaults and every bad-row kind;
entity scheme override and one company per entity; the MCA CIN; changed bytes and
idempotent replay; inline single file in a subfolder; zip with an `.xml` instance
beside linkbases and a catalog; seven invalid packages (two instances, none, broken
zip, traversal member, non-XBRL `.xml`, `.txt`, `.pdf`) with quarantine expectations;
escaping file and directory symlinks, size, missing file, Windows path, the root
itself, and a symlink that stays inside; `contained_path` cases; a removed row and a
foreign filing; fiscal focus; stored packages materialize their instance through
`FilingPackageDownloader`; the CLI (problem lines, summary, exit codes, no control
repository); no network policy; fail-closed prefixes; settings; `runtime_services`
wiring with no client or lock. No existing test changed.

**Commands** (from `sec-xbrl-warehouse/`, the main checkout's venv and `.env`):

| Command | Result |
| --- | --- |
| `pytest -c pyproject.toml --rootdir . ../Implementations/21-file-inbox-india/acceptance` (3 runs) | 8 passed every run |
| `python -m compileall -q src tests scripts` | pass |
| `ruff check .` / `ruff format --check .` | pass / 194 files formatted |
| `mypy src` | pass (90 files) |
| `pytest -m "not integration"` with the live `.env` | 579 passed, 1 failed (`test_repository_registry_entries_are_on_allowed_hosts`, deviation 1), 1 skipped |
| same with `XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS` set to the repository default | 580 passed, 1 skipped (sec-2024.zip not installed) |
| `python scripts/scan_stubs.py` | 0 markers |
| `RUN_XBRL_INTEGRATION=1 pytest -m integration` | 52 passed |

**Deviations.**

1. As in plans 19 and 20: the live `.env` pins the pre-plan-19 taxonomy host list, so
   the existing registry test fails with it and passes with the default. This plan
   doesn't change the allowlist.
2. Additions beyond the contract: `InboxProfile.taxonomy_prefixes` (a defaulted fifth
   field, so a four-argument profile still works), `INBOX_PROFILES`, `read_index`,
   `contained_path` and `file_problems` (shared by discovery, fetch and `inbox check`),
   validation of `fiscal_period`, `entity_scheme` and `source_url`, the index itself
   having to resolve inside the folder and fit `response_max_bytes`, and quarantine of
   packages that fail the instance or archive checks.
3. `source_url` is stored as `FilingRef.primary_document_url` (provenance in the
   `filings` table; nothing fetches it). Member `source_url` and object metadata use
   the `inbox://` URL.
4. A malformed `sha256` value doesn't skip the row at discovery; `inbox check` reports
   it and fetch refuses it.

**Limitations.**

- No real NSE or MCA file was read. The instance rules follow the contract; the
  taxonomy prefixes are guesses marked unverified until the canary.
- `.htm` isn't an instance suffix (the contract lists `.xhtml`/`.html`).
- Fetch failures for inbox files are permanent; after fixing the row or file the
  operator retries the job (`jobs retry`).
- Discovery reads the whole index (bounded by `response_max_bytes`) on each run and
  fetch reads it once per filing.

**Left for the operator** (each step that downloads anything or starts a worker needs
approval):

1. Confirm the terms under which NSE/BSE results and MCA AOC-4 XBRL may be obtained
   and loaded; never crawl the sites.
2. Pin the taxonomies: obtain the NSE/BSE financial results taxonomy and the MCA Ind
   AS taxonomy packages from their publishers, record SHA-256 and size as pinned
   entries in `config/taxonomy_packages.yml`, narrow `source_required_prefixes.nse` and
   `.mca` to the schema locations the files reference, add the confirmed hosts to
   `XBRL_ARELLE_ALLOWED_TAXONOMY_HOSTS` and `network_policy`, then run `taxonomy sync`
   and `taxonomy status` until nothing is `uncovered`.
3. Place files and `index.csv` in `XBRL_INBOX_DIR/<code>/` (Compose: the `app_data`
   volume at `/app/data/inbox`) and run `inbox check --source <code>` until it reports
   no problems.
4. AC-7 canary: enable `nse`, discover, fetch and parse three NSE results files with
   `XBRL_ARELLE_OFFLINE=true`, and confirm their facts (OPERATIONS.md, "Operator file
   inbox").

### Verifier record
