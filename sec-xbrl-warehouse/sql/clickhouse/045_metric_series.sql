-- Standardized metric series (rebuilt per company from canonical_facts) and a
-- read-only mirror of the PostgreSQL companies table for ticker lookups.

-- One row per series point version. `view` is latest or as_reported; `known_from`
-- is when this version became known (as-of queries take the newest version with
-- known_from <= cutoff); each rebuild writes a new `built_at`, and readers use the
-- company's newest build.
CREATE TABLE IF NOT EXISTS {{database}}.metric_series
(
    cik UInt64,
    metric LowCardinality(String),
    view LowCardinality(String),
    period_type LowCardinality(String),
    fiscal_year Nullable(UInt16),
    fiscal_quarter Nullable(UInt8),
    period_start Nullable(Date),
    period_end Date,
    value Decimal256(18),
    unit LowCardinality(Nullable(String)),
    derivation LowCardinality(String),
    concept_qname Nullable(String),
    source_accessions Array(String),
    available_at Nullable(DateTime64(3, 'UTC')),
    known_from DateTime64(3, 'UTC'),
    built_at DateTime64(3, 'UTC')
)
ENGINE = MergeTree
ORDER BY (cik, metric, view, period_type, period_end, known_from, built_at)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {{database}}.companies
(
    cik UInt64,
    name String,
    tickers Array(String),
    exchanges Array(String),
    sic Nullable(UInt32),
    fiscal_year_end Nullable(String),
    updated_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY cik;
