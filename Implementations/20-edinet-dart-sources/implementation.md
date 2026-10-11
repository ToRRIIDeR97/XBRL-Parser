# 20 — Japan EDINET and Korea DART sources

Status: draft for review (11 October 2026). Depends on plans 17 and 18. Independent of
plan 19 (can run before or after it).

## Problem

Japanese issuers file securities reports with the FSA's EDINET, and Korean issuers
with the FSS's DART. Both regulators publish free APIs with XBRL downloads, gated by a
per-user key. The warehouse has no adapter for either.

What the plan relies on (checked 11 October 2026; items marked *verify* are confirmed
in step 0 against the official specifications before coding):

- **EDINET API v2.** `GET https://api.edinet-fsa.go.jp/api/v2/documents.json?date=YYYY-MM-DD&type=2&Subscription-Key=…`
  lists one day's documents (`docID`, `edinetCode`, `filerName`, `docTypeCode`,
  `periodStart`, `periodEnd`, `submitDateTime`, `xbrlFlag`, `withdrawalStatus`).
  `GET …/api/v2/documents/{docID}?type=1&Subscription-Key=…` returns the XBRL zip;
  errors can come back as JSON with HTTP 200. Specification: "EDINET API仕様書
  (Version 2)", FSA, October 2025. Document type codes 120/130 (annual/amended),
  140/150 (quarterly), 160/170 (semi-annual): *verify*.
- **OpenDART.** `GET https://opendart.fss.or.kr/api/list.json` (`crtfc_key`, `bgn_de`,
  `end_de`, `pblntf_ty=A` periodic reports, `page_no`, `page_count`, optional
  `corp_code`), status `000` OK, `013` no data, `020` daily limit (20,000 calls per
  key), `010`/`011` key problems. The original-XBRL download
  `GET /api/fnlttXbrl.xml?crtfc_key&rcept_no&reprt_code` returns a zip: *verify*
  (endpoint name and response). Keys may not be shared (terms).

### Before / after

| | Before | After |
|---|---|---|
| Toyota FY2023 securities report | unreachable | `edinet-S100TR7I`, form `120`, FY 2024 (period end 2024-03-31) |
| Samsung 2023 business report | unreachable | `dart-20240312000736`, form `11011`, FY 2023 |
| A key in a stored URL | n/a | never: keys only in the outgoing request |

## Scope

Contract surface:

- Settings: `edinet_api_key`, `dart_api_key` (`SecretStr | None`,
  `XBRL_EDINET_API_KEY`, `XBRL_DART_API_KEY`), `edinet_max_days` (31),
  `edinet_doc_types` (`120,130,140,150,160,170`), `dart_page_count` (100).
- `exceptions.py`: `SourceRateLimited(SourceAccessError)`, retryable in
  `is_retryable_exception` (like SEC deferrals).
- `sources/edinet.py`: `EDINET_POLICY` (`edinet`, hosts `{"api.edinet-fsa.go.jp"}`, ≤ 1
  request/s), `EDINET_SCHEME = "http://disclosure.edinet-fsa.go.jp"`,
  `EdinetSourceAdapter(settings, http, object_store, entity_keys)`.
  - discover: both bounds required, span ≤ `edinet_max_days` (`ValueError`
    otherwise); one list call per day in order; keep `xbrlFlag == "1"`,
    `withdrawalStatus == "0"`, `docTypeCode` in `request.forms` or the configured
    types, `edinetCode == request.entity` when set. `FilingRef`: `accession =
    "edinet-" + docID`, `cik = resolve_entity_key(EDINET_SCHEME, edinetCode)`, `form =
    docTypeCode`, `filed_date` = submit date, `report_date = periodEnd`,
    `is_amendment` for 130/150/170, `archive_directory_url` without the key.
    `metadata.status != "200"` → `SourceAccessError`.
  - download: zip (`type=1`); a non-zip body → `SourceAccessError`; existing zip
    limits; exactly one entrypoint: the `XBRL/PublicDoc/*.xbrl` instance (AuditDoc
    excluded), else `ArtifactIntegrityError`.
  - fiscal_focus: `jpdei_cor:CurrentFiscalYearEndDateDEI` year and
    `TypeOfCurrentPeriodDEI` (`FY`, `Q1`–`Q3`, `HY` → `H1`); without them, `FY` and the
    `report_date` year for 120/130, else unchanged.
- `sources/dart.py`: `DART_POLICY` (`dart`, hosts `{"opendart.fss.or.kr"}`, ≤ 1
  request/s), `DART_SCHEME = "https://opendart.fss.or.kr/corp_code"`,
  `DartSourceAdapter(settings, http, object_store, entity_keys)`.
  - discover: `list.json` with the window as `bgn_de`/`end_de`, `pblntf_ty=A`,
    `corp_code` when `request.entity` is set, all pages to `total_page`. Without
    `corp_code`, OpenDART searches at most 3 months, so a longer window is split into
    consecutive requests, each from its start date to the day before the same day three
    calendar months later (or `filed_to`), in order. With `corp_code` the window is sent
    whole. Keep
    사업보고서 (11011), 반기보고서 (11012), 분기보고서 (11013 for a `.03` period,
    11014 for `.09`); a `[기재정정]` prefix sets `is_amendment`; other reports are
    skipped. `FilingRef`: `accession = "dart-" + rcept_no`, `cik =
    resolve_entity_key(DART_SCHEME, corp_code)`, `form` = report code, `filed_date =
    rcept_dt`, `report_date` = last day of the `(YYYY.MM)` month. Status `013` → empty
    result; `020` → `SourceRateLimited`; other non-`000` → `SourceAccessError`.
  - download: `fnlttXbrl.xml` with `rcept_no` and `reprt_code`; a non-zip body →
    `SourceAccessError` (or `SourceRateLimited` for `020`); one `.xbrl` entrypoint.
  - fiscal_focus: 11011 `FY`, 11012 `H1`, 11013 `Q1`, 11014 `Q3`; year =
    `report_date` year.
- **Secrets (AC-3).** Keys are sent only as the regulator requires and are absent
  from `FilingRef`, manifests (`source_url`), object keys, job payloads, log records
  and exception messages (redact URLs before logging or raising). A missing key raises
  `ConfigurationError` before any request.
- **Taxonomies (AC-8).** Pinned EDINET taxonomy packages (FSA publishes yearly
  packages) and the taxonomies DART filings reference; `source_required_prefixes` for
  `edinet` and `dart`; taxonomy host allowlist additions recorded in SECURITY.md.
  Hashes come from an operator download (approval needed).
- Docs: DATA_MODEL (keys, forms, fiscal rules), OPERATIONS (keys, daily limits, one
  fetch worker per source), SECURITY (secrets, hosts, terms), `.env.example`.

Exclusions: EDINET CSV (`type=5`) and English documents, DART's JSON financial
statement APIs (`fnlttSinglAcntAll`), Japanese/Korean label translation, non-periodic
filings.

Invariants: keys never persisted or logged; each adapter only reaches its own host;
SEC behavior unchanged.

![EDINET and DART APIs feeding their adapters, the secrets rule, and the unchanged registry, storage and parse path with operator-pinned taxonomies, linked to AC-1 to AC-9](diagrams/edinet-dart.svg)

## Acceptance criteria

- **AC-1 EDINET discovery.** Per-day listing, filters, mapping, window bounds, missing
  key refusal.
- **AC-2 EDINET package.** Single PublicDoc instance entrypoint; JSON error body is an
  access error; missing instance rejected.
- **AC-3 Secrets.** No key in filings, manifests, object keys, errors or logs.
- **AC-4 DART discovery.** Report-code and amendment mapping, paging, entity filter,
  status `013`/`020`/others.
- **AC-5 DART package.** Original XBRL zip, single `.xbrl` entrypoint, missing
  instance rejected.
- **AC-6 Fiscal fields.** EDINET jpdei with fallback; DART from the report code.
- **AC-7 Policies.** Hosts and rates.
- **AC-8 Taxonomies (manual + review).** Pins and coverage; `taxonomy status` clean
  with both sources enabled.
- **AC-9 Canary (manual, operator).** With approval and the user's own keys: one week
  of each source, three filings each parsed offline.
- **AC-10 Regression.** Validate gates.

| Criterion | Acceptance | Verification |
|---|---|---|
| AC-1 | day listing, filters, mapping | no key, window bounds, withdrawn, amended, forms/entity |
| AC-2 | entrypoint, download URL | JSON body, no instance |
| AC-3 | success path + EDINET error | DART error path and logs |
| AC-4 | mapping, 013, 020 | paging, Q3, corp_code |
| AC-5 | download and entrypoint | no instance |
| AC-6 | fallbacks | jpdei facts, `HY` → `H1` |
| AC-7 | hosts and rates | — |
| AC-8–AC-9 | manual | manual |

## Implementation sequence

0. Verify the *verify* items against the official specifications (no keys needed to
   read them); record differences here before coding.
1. Settings, `SourceRateLimited`, policies (AC-7).
2. EDINET adapter (AC-1, AC-2, AC-6) with secret redaction (AC-3).
3. DART adapter (AC-4, AC-5, AC-6).
4. Registry wiring, taxonomy entries (AC-8, with operator pins). Docs. Validate.

## Verification

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/20-edinet-dart-sources/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/20-edinet-dart-sources/verification
```

No live calls (canned responses through `httpx.MockTransport`). The verification set
imports `acceptance/api_fixtures.py`.

### Baseline (plan 17 candidate 131a87f, 11 October 2026)

All tests fail at import (`sec_xbrl_warehouse.sources` from plan 18 is missing).
Rerun on the accepted plan 18 commit before implementation.

### Plan-author correction (11 October 2026)

The implementer's step 0 found that OpenDART limits searches without `corp_code` to
3 months, while `test_ac4_dart_discovery` pinned one 4-month request. The test was
wrong against the official specification. The contract now splits long windows (above),
the acceptance test uses a 3-month window (2024-03-01 to 2024-05-31, amendment row
dated 2024-05-20), and the verification set gained a splitting test. No other
assertion changed.

## Completion evidence

### Implementer record

### Verifier record
