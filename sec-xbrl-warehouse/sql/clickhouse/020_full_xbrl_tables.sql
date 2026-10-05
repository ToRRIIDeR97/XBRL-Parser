-- Identity hashes (fact_id, context_hash, ...) and _row_hash are SHA-256 digests stored
-- as 32 raw bytes (FixedString(32)); the v_* views return them as lower-case hex.
-- raw_value is NULL when it equals the typed value; raw_value_scale then records how to
-- render it (see docs/DATA_MODEL.md). Silver Parquet keeps hex and every raw value.

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_contexts
(
    accession String,
    cik UInt64,
    context_id String,
    context_hash FixedString(32),
    entity_scheme String,
    entity_identifier String,
    period_kind LowCardinality(String),
    period_start Nullable(Date),
    period_end Nullable(Date),
    period_instant Nullable(Date),
    dimensions_json String CODEC(ZSTD(6)),
    segment_xml Nullable(String) CODEC(ZSTD(6)),
    scenario_xml Nullable(String) CODEC(ZSTD(6)),
    source_document String,
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
PARTITION BY toUInt16OrZero(substring(accession, 12, 2))
ORDER BY (accession, extraction_revision, source_document, context_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_concepts
(
    accession String,
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
    is_extension UInt8,
    labels_json String CODEC(ZSTD(6)),
    documentation Nullable(String) CODEC(ZSTD(6)),
    references_json Nullable(String) CODEC(ZSTD(6)),
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, concept_namespace, concept_name)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_facts
(
    fact_id FixedString(32),
    accession String,
    cik UInt64,
    company_name String,
    form LowCardinality(String),
    filed_date Date,
    fiscal_year Nullable(Int16),
    fiscal_period LowCardinality(Nullable(String)),
    accepted_at Nullable(DateTime64(3, 'UTC')),
    concept_namespace String,
    concept_name String,
    concept_qname String,
    taxonomy_version Nullable(String),
    context_id String,
    context_hash FixedString(32),
    unit_id Nullable(String),
    unit_numerator_json Nullable(String),
    unit_denominator_json Nullable(String),
    raw_value Nullable(String) CODEC(ZSTD(6)),
    raw_value_scale Nullable(UInt8),
    value_decimal Nullable(Decimal256(18)),
    value_text Nullable(String) CODEC(ZSTD(6)),
    value_boolean Nullable(UInt8),
    value_date Nullable(Date),
    is_nil UInt8,
    decimals Nullable(Int32),
    precision Nullable(Int32),
    language LowCardinality(Nullable(String)),
    period_kind LowCardinality(String),
    period_start Nullable(Date),
    period_end Nullable(Date),
    period_instant Nullable(Date),
    entity_scheme String,
    entity_identifier String,
    dimensions_json String CODEC(ZSTD(6)),
    dimension_hash FixedString(32),
    tuple_path Nullable(String),
    duplicate_ordinal UInt16,
    duplicate_set_hash FixedString(32),
    footnote_ids Array(FixedString(32)),
    source_document String,
    source_fact_id Nullable(String),
    source_attributes_json String DEFAULT '{}' CODEC(ZSTD(6)),
    value_kind LowCardinality(String) DEFAULT 'text',
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
PARTITION BY toYear(filed_date)
ORDER BY (cik, concept_namespace, concept_name, ifNull(period_end, toDate('1970-01-01')), ifNull(period_instant, toDate('1970-01-01')), accession, extraction_revision, context_hash, fact_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_relationships
(
    relationship_id FixedString(32),
    accession String,
    network_type LowCardinality(String),
    link_role_uri String,
    arc_role_uri String,
    from_qname String,
    to_qname String,
    arc_order Decimal64(8),
    priority Nullable(Int32),
    weight Nullable(Decimal64(8)),
    preferred_label Nullable(String),
    use LowCardinality(Nullable(String)),
    closed Nullable(UInt8),
    usable Nullable(UInt8),
    target_role Nullable(String),
    context_element LowCardinality(Nullable(String)),
    attributes_json Nullable(String) CODEC(ZSTD(6)),
    source_document Nullable(String),
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, network_type, link_role_uri, from_qname, to_qname, relationship_id)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_footnotes
(
    footnote_id FixedString(32),
    accession String,
    role_uri Nullable(String),
    language LowCardinality(Nullable(String)),
    text String CODEC(ZSTD(6)),
    html Nullable(String) CODEC(ZSTD(6)),
    linked_fact_ids Array(FixedString(32)),
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, footnote_id)
SETTINGS index_granularity = 8192;

-- Written last: readers never observe a partially loaded extraction revision.
CREATE TABLE IF NOT EXISTS {{database}}.xbrl_extractions
(
    accession String,
    extraction_revision UInt32,
    entity_counts_json String,
    metadata_json String CODEC(ZSTD(6)),
    completed_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(completed_at)
ORDER BY (accession, extraction_revision);
