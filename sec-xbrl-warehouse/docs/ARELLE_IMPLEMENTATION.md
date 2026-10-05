# Arelle Adapter Implementation Guide

The adapter boundary is `infrastructure/xbrl/arelle_adapter.py`. Arelle objects must not leave this module.

## Loading

The implementation should:

1. Create a headless Arelle controller.
2. Configure web cache, taxonomy packages, plugins, disclosure system, and logging.
3. Load the filing entry point or inline document set.
4. Optionally run XBRL and EFM validation according to configuration.
5. Extract domain records.
6. Close `modelXbrl` and controller resources in `finally` blocks.

Do not assume the primary HTML document is the only document containing inline facts. Preserve and load the entire filing package so Arelle can resolve schemas, linkbases, and multi-document inline sets.

## SEC inline transformations

EDGAR filings use the SEC `ixt-sec` transformation registry for cover-page and some
numeric facts. `_create_controller` always loads the in-repo plugin
`infrastructure/xbrl/sec_transforms.py` by path, independent of
`XBRL_ARELLE_PLUGIN_PATHS`. The plugin registers its functions through Arelle's
`ModelManager.LoadCustomTransforms` hook. Controller creation fails with
`XbrlExtractionError` if the plugin can't be loaded or any of its transforms is
missing afterward.

- Registered transforms: `boolballotbox`, `boolfalse`, `booltrue`, `countrynameen`,
  `stateprovnameen`, `edgarprovcountryen`, `exchnameen`, `entityfilercategoryen`,
  `numwordsen`, `numinf`, `numneginf`, `numnan`, `duryear`, `durmonth`, `durweek`,
  `durday`, `durhour`, `durwordsen`, `datequarterend`.
- Arelle still applies each transform and validates the result against the concept
  type. The plugin only maps displayed text to an XBRL lexical value.
- Input a transform can't map raises Arelle's `FunctionArgType`, which Arelle reports
  as `transformValueError`. A format in the `ixt-sec` namespace that isn't registered
  is reported as `invalidTransformation`.
- The fact keeps its displayed text in `raw_value`. Its typed value is `None`; the
  `(ixTransformValueError)` sentinel is never stored.
- `edgarprovcountryen` covers US states, Canadian provinces, and a reviewed subset of
  EDGAR country codes. Other names fail with a diagnostic rather than a guessed code.
- `metadata.provenance["transform_registries"]` lists every available registry as
  `{"namespace", "version", "source"}`: the standard `ixt` registries from
  `arelle-release` and the `ixt-sec` plugin version.

To update the plugin, change `sec_transforms.py`, bump `SEC_TRANSFORM_REGISTRY_VERSION`
and `CURRENT_EXTRACTION_REVISION`, extend `tests/unit/test_sec_transforms.py`, and
re-extract the affected filings.

## Taxonomy cache and network policy

- Store Arelle's web cache under `XBRL_ARELLE_CACHE_DIR`; do not use a user-global cache in workers or tests.
- Put reviewed, checksum-pinned taxonomy packages under `XBRL_ARELLE_TAXONOMY_PACKAGES_DIR` and register them in `config/taxonomy_packages.yml`.
- Filing-package documents must resolve locally from immutable bronze bytes. Controlled cache fills may be allowed only for standard taxonomy hosts listed in configuration.
- `XBRL_ARELLE_OFFLINE=true` must make any uncached remote dependency a deterministic failure, not an implicit network request.
- Record Arelle, plugin, disclosure-system, taxonomy-package, and extraction-schema versions with every extraction revision.

## Context extraction

For every context capture:

- context ID;
- entity identifier scheme and value;
- instant or start/end period;
- scenario and segment content;
- explicit dimensions: axis QName and member QName;
- typed dimensions: axis QName, serialized typed member XML, and a stable hash;
- canonical dimension ordering for a deterministic context hash.

## Unit extraction

Capture numerator and denominator measures. Monetary units must retain currency QName/code; shares and pure units must remain distinguishable.

## Fact extraction

For each fact:

- determine concept and context;
- preserve raw lexical text where possible;
- produce typed values without losing decimal precision;
- store nil, decimals, precision, language, unit, tuple ancestry, source document, and inline ID;
- assign a duplicate ordinal within facts sharing the same concept/context/unit/value identity;
- derive a deterministic fact ID only after all identity fields are stable.

## Concept extraction

Capture concepts actually used by facts plus concepts referenced by presentation/calculation/definition networks. Preserve labels by role/language rather than flattening to one label.

## Relationship extraction

Iterate relationship sets for:

- parent-child presentation;
- summation-item calculation;
- dimensional definition networks;
- labels/references;
- footnotes.

Record link role and arc role on every relationship. Calculation weight is decimal, not merely a boolean negative flag.

## Validation messages

Store original Arelle code, severity, message, model object references, source file/line when available, and structured arguments. A normalized severity is useful, but the original code/message must remain available.

## Test strategy

Start with the synthetic fixture, then maintain a pinned corpus manifest of real SEC accessions and expected structural counts. Do not commit copyrighted filing content if repository policy disallows it; tests can download/cache with an explicit SEC identity or use internally retained fixtures.
