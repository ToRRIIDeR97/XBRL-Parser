-- Standard taxonomy concepts (us-gaap, dei, srt, ...) stored once per concept and
-- taxonomy version instead of once per filing; xbrl_concepts keeps each filing's
-- extension concepts. Attributes and labels come from the most recently written filing.

CREATE TABLE IF NOT EXISTS {{database}}.taxonomy_concepts
(
    concept_namespace String,
    concept_name String,
    concept_qname String,
    taxonomy_version Nullable(String),
    datatype_qname Nullable(String),
    substitution_group_qname Nullable(String),
    period_type LowCardinality(Nullable(String)),
    balance LowCardinality(Nullable(String)),
    abstract UInt8,
    nillable UInt8,
    labels_json String CODEC(ZSTD(6)),
    documentation Nullable(String) CODEC(ZSTD(6)),
    references_json Nullable(String) CODEC(ZSTD(6)),
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_ingested_at)
ORDER BY (concept_namespace, concept_name, ifNull(taxonomy_version, ''))
SETTINGS index_granularity = 8192;
