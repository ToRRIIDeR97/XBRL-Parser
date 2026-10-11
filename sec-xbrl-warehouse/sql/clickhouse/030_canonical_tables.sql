-- One row per (raw fact, metric, extraction revision): the sort key leaves out the
-- mapping, so a remap's row replaces the older mapping's row on merge
-- (ReplacingMergeTree keeps the highest mapping_version), the same row readers
-- pick with `ORDER BY mapping_version DESC LIMIT 1 BY`. Mapping history stays in
-- PostgreSQL `concept_mappings`.
CREATE TABLE IF NOT EXISTS {{database}}.canonical_facts
(
    canonical_fact_id FixedString(32),
    raw_fact_id FixedString(32),
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
    accepted_at Nullable(DateTime64(3, 'UTC')),
    concept_namespace Nullable(String),
    concept_qname Nullable(String),
    fiscal_year Nullable(UInt16),
    fiscal_period LowCardinality(Nullable(String)),
    report_date Nullable(Date),
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC'),
    INDEX idx_accession accession TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(mapping_version)
PARTITION BY toYear(filed_date)
ORDER BY (cik, canonical_metric, ifNull(period_end, toDate('1970-01-01')), ifNull(period_instant, toDate('1970-01-01')), accession, extraction_revision, raw_fact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';
