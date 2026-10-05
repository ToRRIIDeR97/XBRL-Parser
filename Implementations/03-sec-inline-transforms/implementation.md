# 03 — Load the SEC inline XBRL transformation rules

Status: plan only. Code follows after this plan is reviewed.

## Problem

SEC filings format many cover-page and some numeric facts with the SEC's own inline
transformation registry (`ixt-sec`, namespace
`http://www.sec.gov/inlineXBRL/transformation/2015-08-31`). The installed Arelle
(`arelle-release` 2.44.4) doesn't include that registry. The SEC registry ships with
Arelle's separate EDGAR plugin repository. `validate/EFM` isn't installed either.

What happens today (reproduced with `fixtures/sec-transforms`):

- Checkbox, state, country and exchange facts get the literal text
  `(ixTransformValueError)` as their value.
- Numeric facts using `ixt-sec` transforms (for example `numwordsen`) are stored with
  **no value at all**.
- Every such fact logs `ix11.1x.1.2:invalidTransformation`. The 2 October AAPL load
  logged 1,600 of these.

Real AAPL examples: `dei:DocumentAnnualReport`, `dei:EntityFilerCategory`,
`dei:EntityIncorporationStateCountryCode` and `dei:EntityAddressStateOrProvince`
are all `(ixTransformValueError)`.

### Before / after (fixture values)

| Fact (format) | Before | After |
|---|---|---|
| AnnualReport (`ixt-sec:boolballotbox`, ☒) | text `(ixTransformValueError)` | boolean `true` |
| QuarterlyReport (`boolballotbox`, ☐) | text `(ixTransformValueError)` | boolean `false` |
| StateOfIncorporation (`stateprovnameen`, "California") | `(ixTransformValueError)` | `CA` |
| CountryOfIncorporation (`countrynameen`, "Canada") | `(ixTransformValueError)` | `CA` |
| SecurityExchange (`exchnameen`) | `(ixTransformValueError)` | `NASDAQ` |
| Shares (`numwordsen`, "twelve thousand") | value missing | `12000` |

## Scope

Likely files:

- `src/sec_xbrl_warehouse/infrastructure/xbrl/arelle_adapter.py`: load the SEC
  registry in `_create_controller` by default, independent of
  `XBRL_ARELLE_PLUGIN_PATHS`. Map transform failures to `None` values (raw text
  kept). Record the registry in provenance.
- The registry implementation. Preferred: a pinned, checksum-verified copy of the
  `transform` plugin from the Arelle EDGAR repository (Apache-2.0), under a
  third-party directory with its license and source commit. Fallback, if vendoring
  is rejected in review: an in-repo Arelle plugin that registers the `ixt-sec`
  functions through Arelle's custom-transform hook.
- `docs/DEPENDENCIES.md` and `docs/ARELLE_IMPLEMENTATION.md`: record the source,
  version, license and update procedure.
- `docs/SECURITY.md` if third-party code is vendored (supply-chain note).

Exclusions:

- Enabling `validate/EFM` (the EDGAR Filer Manual rules) is a separate decision.
- No changes to standard `ixt` (XBRL International) handling.

Invariants:

- Arelle remains the only interpreter of inline XBRL (hard constraint 1).
- `raw_value` keeps the displayed text. Transformed values go in the typed value
  fields.
- Controller creation fails fast (`XbrlExtractionError`) if the registry can't be
  loaded. It must not silently fall back to error values.

## Acceptance criteria

- **AC-1 Code and boolean transforms.** For the fixture, `boolballotbox` yields
  `value_kind=boolean` with `true` for ☒ and `false` for ☐. `stateprovnameen`,
  `countrynameen` and `exchnameen` yield the SEC codes (`CA`, `CA`, `NASDAQ`).
  `raw_value` keeps the original text.
- **AC-2 Numeric transforms.** `numwordsen` produces a numeric fact with
  `value_decimal = 12000`.
- **AC-3 No false diagnostics.** Facts using known `ixt-sec` transforms raise no
  `invalidTransformation` issues.
- **AC-4 Unknown transforms.** A format in the `ixt-sec` namespace that the registry
  doesn't define (fixture fact `ex:Bogus`) is still reported as
  `invalidTransformation`. Its typed value is `None` and `raw_value` is kept. No
  stored fact ever contains the literal `(ixTransformValueError)`.
- **AC-5 Always on.** The registry loads with `arelle_plugin_paths=""`. Operators
  don't need to configure it.
- **AC-6 Provenance.** `metadata.provenance["transform_registries"]` lists each loaded
  registry as `{"namespace": ..., "version": ...}`, including the `ixt-sec`
  namespace and the pinned source version.

## Implementation sequence

1. Decide vendoring versus in-repo plugin in review of this plan. Record license and
   commit.
2. Load the registry in `_create_controller` and add it to provenance (AC-5, AC-6).
3. Treat Arelle's transform-error sentinel as "no typed value" in fact conversion
   (AC-4).
4. Bump `extraction_revision` (shared with plans 01 and 02 if they ship together),
   update docs, run the Validate suite.
5. Operator step: re-extract the AAPL filings. Confirm no `(ixTransformValueError)`
   values remain:
   `SELECT count() FROM xbrl.v_xbrl_facts_latest WHERE value_text = '(ixTransformValueError)'`.

## Verification

From `sec-xbrl-warehouse/`:

```bash
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/03-sec-inline-transforms/acceptance
.venv/bin/pytest -c pyproject.toml --rootdir . ../Implementations/03-sec-inline-transforms/verification
```

All tests are `arelle`-marked and run real Arelle on a local fixture. They need
`data/cache/arelle`. No SEC or FASB downloads are required.

### Baseline (main @ 8f16ca0, 5 October 2026)

All 8 tests fail on their assertions:

- Values are `(ixTransformValueError)` or missing.
- `invalidTransformation` is logged for every `ixt-sec` fact.
- Provenance has no `transform_registries` key.

## Completion evidence

To be filled in by the implementer and the reviewer. The plan author wrote both test
sets. If the same agent implements this plan, the verification set isn't
independently authored.
