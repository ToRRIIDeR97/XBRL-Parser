# 22 — Singapore ACRA financial statements (inbox profile)

Status: draft for review (11 October 2026). Depends on plan 21 (inbox adapter), and
through it on plans 17 and 18.

## Problem

Singapore companies file financial statements in XBRL with ACRA (BizFinx). As of 11
October 2026 there is no public ACRA retrieval API: authorised officers of a company
(directors, company secretary, registered filing agent) can retrieve that company's
own XBRL through BizFile+, ACRA's API is for submission, and third-party vendors
resell ACRA XBRL financials. SGX announcements are mostly PDF.

So, like India, Singapore comes in through the operator inbox (plan 21). This plan
adds the ACRA profile, UEN entity keys, and the ACRA taxonomy pins.

### Before / after

| | Before | After |
|---|---|---|
| A company's FY2024 XBRL statements (from its officers or a vendor) | can't be loaded | `index.csv` row → `acra-<id>`, entity key from UEN `201912345K` |
| A bad UEN in the index | n/a | row skipped; `inbox check --source acra` reports it |

## Scope

- `sources/inbox.py`: `ACRA_PROFILE = InboxProfile(code="acra",
  entity_scheme="https://www.acra.gov.sg/uen", entity_pattern=UEN pattern,
  default_fiscal_period="FY")`. UEN pattern:
  `^([0-9]{8}[A-Z]|[0-9]{9}[A-Z]|[TSR][0-9]{2}[A-Z]{2}[0-9]{4}[A-Z])$` (business,
  local company, other entities).
- `inbox check` and `runtime_services` know the `acra` profile; `acra` is a valid
  `enabled_sources` value.
- Taxonomies (AC-5): pins for the ACRA taxonomy versions in use (operator download
  with approval) and `source_required_prefixes.acra`; host allowlist addition in
  SECURITY.md.
- Docs: OPERATIONS (how ACRA files may be obtained, the inbox steps, the terms gate),
  DATA_MODEL (UEN keys), SECURITY.

Exclusions: any BizFile+ automation (it needs a company officer's Singpass login,
which the warehouse must never hold), SGX announcements, vendor API integrations
(a later plan once a vendor and licence are chosen).

Invariants: as plan 21; no credentials for ACRA systems are stored or used.

![ACRA profile feeding the plan 21 inbox adapter, with the operator's lawful file supply, UEN validation, inbox check, storage under bronze/acra, ACRA taxonomy pins, and the enablement gate, linked to AC-1 to AC-6](diagrams/acra-profile.svg)

## Acceptance criteria

- **AC-1 Profile.** Code, scheme, default fiscal period; `acra` accepted in
  `enabled_sources`.
- **AC-2 UEN keys.** Valid UENs of all three shapes become entity keys; malformed or
  lower-case UENs are skipped; FY and the period-end year (June year-ends included).
- **AC-3 Package.** Stored under `bronze/acra/filings/`, instance entrypoint.
- **AC-4 `inbox check`.** Works for `acra`.
- **AC-5 Taxonomies (manual + review).**
- **AC-6 Gate (manual).** The operator records how files are obtained and confirms
  redistribution terms before enabling `acra` outside local testing.
- **AC-7 Regression.** Validate gates.

| Criterion | Acceptance | Verification |
|---|---|---|
| AC-1 | profile | enable setting |
| AC-2 | one valid, one invalid | three shapes, lower case, wrong prefix, empty |
| AC-3 | package | — |
| AC-4 | — | CLI |
| AC-5–AC-6 | manual | manual |

## Implementation sequence

1. `ACRA_PROFILE` and wiring (AC-1, AC-2, AC-4).
2. Taxonomy pins (AC-5, operator). Docs. Validate.

## Verification

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/22-singapore-acra/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/22-singapore-acra/verification
```

`acceptance/inbox_fixtures.py` is a copy of plan 21's.

### Baseline (plan 17 candidate 131a87f, 11 October 2026)

All tests fail at import (`sec_xbrl_warehouse.sources`, plans 18 and 21). Rerun on
the accepted plan 21 commit before implementation.

## Completion evidence

### Implementer record

### Verifier record
