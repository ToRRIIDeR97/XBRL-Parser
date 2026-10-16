# 01 — Download only the files Arelle needs

Status: implemented and merged to main.

## Problem

`select_package_files` ([filing_downloader.py:342](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/ingestion/filing_downloader.py)) keeps every `.htm`/`.html` in the
accession directory. `ArelleExtractor._load_model` ([arelle_adapter.py:279](../../sec-xbrl-warehouse/src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py)) then
puts every `.htm` in the package into the inline XBRL document set (IXDS).

Measured on the 2 October AAPL load:

- One 10-Q package had 59 members: 52 rendered `R*.htm` pages, 3 exhibits, 2 index
  pages, and about 8 files Arelle actually needs.
- Exhibits loaded into the IXDS produced 119,117 `xmlSchema:syntax` errors (73% of
  `validation_issues`) and 3,626 `arelle:nonIxdsDocument` warnings.
- Files are downloaded one at a time in a `for` loop even though
  `sec_max_concurrency` defaults to 4. At 8 requests/second, 59 files cost at least
  7.4 seconds per filing.
- AAPL's January 2019 10-Q (`0000320193-19-000010`) can't be ingested: its
  `index.json` lists only the `.txt` bundle and `-xbrl.zip`.

### Before / after

| | Before | After |
|---|---|---|
| Files fetched for an AAPL 10-Q | 59 | about 8 |
| IXDS documents | every `.htm` in the folder | inline XBRL documents only |
| Filing with only `.txt` + `-xbrl.zip` | fails (no entrypoint) | extracted from the zip |
| Download pattern | serial | concurrent, bounded by `sec_max_concurrency` |

## Scope

Likely files:

- `src/sec_xbrl_warehouse/ingestion/filing_downloader.py`: selection, concurrent
  download, zip fallback, cache key.
- `src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py`: `_load_model`
  IXDS membership.
- `tests/unit/test_filing_downloader.py`: `test_select_package_files_includes_xbrl_and_primary`
  asserts `report.html` is selected. That assertion encodes the old policy and must
  change to assert it is excluded.
- `docs/ARCHITECTURE.md` (bronze section) and `docs/INGESTION_RUNBOOK.md`: describe
  the selection policy and the zip fallback.

Interfaces to keep:

- `select_package_files(index_payload, filing)` keeps its positional signature. It
  gains an optional keyword `filing_summary: bytes | None = None`.
- `FilingPackageDownloader.download_package(filing)` and `materialize_package(manifest, destination)`.
- `FilingPackageManifest` / `PackageMember` stay backward-readable. Old manifests must
  still deserialize.

Exclusions:

- Text extraction from the primary document (MD&A, risk factors) is out of scope.
  The primary document is still downloaded and retained, so that work needs no
  re-download.
- No deletion of previously stored bronze objects (hard constraint 7).

Invariants:

- Raw bytes are stored immutably under checksum-qualified keys.
- All existing download limits apply to every path: `filing_max_members`,
  `filing_max_bytes`, `response_max_bytes`, plus zip limits
  (`zip_max_members`, `zip_max_member_bytes`, `zip_max_total_bytes`,
  `zip_max_compression_ratio`) and path-traversal checks for the archive fallback.
- The SEC client remains the only network path (rate limiter, host allowlist).
- Changing package composition changes extraction input, so it ships with an
  `extraction_revision` bump. Re-extraction of the 43 AAPL filings is an operator
  step after merge.

## Acceptance criteria

- **AC-1 Selection.** From an `index.json` listing, keep: the primary document, the
  instance document, `*.xsd`, XBRL instance/linkbase `*.xml`, `FilingSummary.xml`
  (any case) and `MetaLinks.json`. Drop: rendered reports `R<n>.htm` and legacy
  `R<n>.xml`, `*-index.htm(l)`, `*-index-headers.html`, the `.txt` full submission,
  images, scripts, stylesheets, spreadsheets, and `.htm` documents that aren't XBRL
  inputs (exhibits). A primary document whose name merely starts with `r` (for
  example `r10q.htm`) is kept.
- **AC-2 Multi-document inline sets.** When `FilingSummary.xml` is present, its
  `<InputFiles>` list decides which additional `.htm` documents are XBRL inputs.
  The downloader fetches `FilingSummary.xml` once, before the remaining selection.
  Without it, only the primary `.htm` is an inline candidate.
- **AC-3 IXDS membership.** `ArelleExtractor.extract` builds the inline document set
  only from inline XBRL documents in the package (documents declaring the inline
  XBRL namespace). Non-inline `.htm` files present in the directory are not loaded.
  They produce no `xmlSchema:syntax` or `arelle:nonIxdsDocument` issues and don't
  appear in `metadata.source_documents`. Facts from the inline documents are
  unchanged.
- **AC-4 Archive fallback.** When the index lists none of the needed XBRL inputs
  but lists `<accession>-xbrl.zip`, selection returns only that archive. The
  downloader fetches it (not the `.txt`), safely expands it, and stores each member
  under its checksum key. `materialize_package` returns the instance document from
  the archive as the entrypoint. The manifest records the archive as the source of
  those members.
- **AC-5 Bounded concurrency.** Members are downloaded concurrently, never more than
  `settings.sec_max_concurrency` at once. Manifest member order is sorted by name
  regardless of completion order.
- **AC-6 Previously cached manifests.** A manifest cached under the old selection
  policy must not cause `ArtifactIntegrityError` on re-ingestion. The cache key
  includes a selection-policy version. Old manifest objects are left in place.
- **AC-7 Limits on the fallback path.** Path-traversal names, too many members, or
  a compression ratio above the configured limit in `-xbrl.zip` raise
  `ArtifactIntegrityError` before any member is published.

## Implementation sequence

1. Selection policy and the `filing_summary` keyword (AC-1, AC-2). Update the old
   unit test's `report.html` assertion.
2. Downloader: fetch `FilingSummary.xml` first when listed, then the rest
   concurrently under a semaphore of `sec_max_concurrency` (AC-2, AC-5).
3. Add the policy version to the manifest cache key (AC-6).
4. Archive fallback with the zip safety checks already used for bulk ZIPs
   (`ingestion/bulk_loader.py` / `sec_tsv.py`) (AC-4, AC-7).
5. `_load_model`: build the IXDS from inline documents only (AC-3).
6. Bump `extraction_revision`, update docs, run the full Validate suite, then
   re-extract the AAPL filings and confirm `xmlSchema:syntax` counts drop.

## Verification

Run from `sec-xbrl-warehouse/` with the Python 3.12 virtualenv:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/01-filing-package-selection/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/01-filing-package-selection/verification
```

The `arelle`-marked tests run real Arelle on `fixtures/inline-package` (an inline 10-K,
a malformed exhibit and an `R2.htm`). They need the local Arelle cache at
`data/cache/arelle`. A `lxml.schemaError` about `http://www.w3.org/2001/xml.xsd`
appears when the w3.org host is blocked. It's environmental, and the tests don't
assert on it.

Then the standard gates in `AGENTS.md` (compileall, ruff, mypy,
`pytest -m "not integration"`, `scan_stubs.py`, integration run).

Manual check after merge: re-extract one AAPL 10-Q and compare member count, the
`xmlSchema:syntax` count, and fact count with the revision-2 values.

### Baseline (main @ 8f16ca0, 5 October 2026)

All 12 tests fail for the intended reasons:

- AC-1: R pages, exhibits and index files are selected.
- AC-2: `filing_summary` keyword missing; the downloader fetches exhibits and `R2.htm`.
- AC-3: `nonIxdsDocument` and `xmlSchema:syntax` present; `ex99.htm` in `source_documents`.
- AC-4: the primary `.htm` is selected instead of the zip.
- AC-5: peak concurrency is 1.
- AC-6: `R2.htm` is reused from the old manifest.
- AC-7: the baseline requests the unlisted primary document (`KeyError` in the fake HTTP client).

## Completion evidence

To be filled in by the implementer and the reviewer: criteria met, commands run,
results, remaining limitations. The plan author wrote both test sets. If the same
agent implements this plan, the verification set isn't independently authored.

### Implementer record (5 October 2026)

- AC-1/AC-2: `select_package_files(..., filing_summary=)` with selection policy 2;
  the downloader fetches `FilingSummary.xml` first, or reads it from a cached manifest.
- AC-3: `_load_model` adds a non-entrypoint `.htm` to the IXDS only when it declares
  the inline XBRL namespace (`_declares_inline_xbrl`, streamed lxml, no entities or network).
- AC-4/AC-7: `-xbrl.zip` fallback. The archive is stored as raw bronze, checked with
  `validate_zip_integrity` plus `filing_max_members`/`filing_max_bytes`, and only then
  are its members published. Members carry `PackageMember.source_archive`.
- AC-5: `asyncio.TaskGroup` under a `sec_max_concurrency` semaphore with a shared
  byte budget; members are sorted by name.
- AC-6: cache key gains `selection-v2/`; old keys are not read or removed.
- `CURRENT_EXTRACTION_REVISION = 3` (`domain/xbrl.py`) is the default for
  `filing enqueue` and filing discovery.
- Gates: compileall, ruff check/format, mypy, `pytest -m "not integration"` (189 passed),
  `scan_stubs.py` (0), integration (23 passed), plan tests (12 passed).
- Not done: re-extracting AAPL and ingesting `0000320193-19-000010` (operator step).
