CREATE TABLE IF NOT EXISTS {{database}}.canonical_facts
(
    canonical_fact_id FixedString(64),
    raw_fact_id FixedString(64),
    accession String,
    cik UInt64,
    filed_date Date,
    form LowCardinality(String),
    canonical_metric LowCardinality(String),
    normalized_value_decimal Nullable(Decimal256(18)),
    normalized_value_text Nullable(String) CODEC(ZSTD(6)),
    normalized_unit LowCardinality(Nullable(String)),
    sign_multiplier Int8,
    period_kind LowCardinality(String),
    period_start Nullable(Date),
    period_end Nullable(Date),
    period_instant Nullable(Date),
    dimensions_json String CODEC(ZSTD(6)),
    mapping_id UUID,
    mapping_version UInt32,
    mapping_confidence Float32,
    mapping_explanation String CODEC(ZSTD(6)),
    extraction_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(mapping_version)
PARTITION BY toYear(filed_date)
ORDER BY (cik, canonical_metric, ifNull(period_end, toDate('1970-01-01')), ifNull(period_instant, toDate('1970-01-01')), accession, raw_fact_id)
SETTINGS index_granularity = 8192;
