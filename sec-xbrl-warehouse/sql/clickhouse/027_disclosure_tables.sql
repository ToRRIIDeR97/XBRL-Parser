-- Cover pages and segment/product/geographic breakdowns (derived from the extraction's
-- dei and dimensional facts by normalization/disclosures.py; rebuildable from silver).

-- One row per filing and revision: the registrant's own non-dimensional dei facts.
-- The security_* arrays list each security reported on the class-of-stock axis.
CREATE TABLE IF NOT EXISTS {{database}}.xbrl_cover_pages
(
    accession String,
    cik UInt64,
    form LowCardinality(String),
    filed_date Date,
    document_type LowCardinality(Nullable(String)),
    period_end_date Nullable(Date),
    registrant_name Nullable(String),
    filer_category LowCardinality(Nullable(String)),
    amendment Nullable(UInt8),
    trading_symbol Nullable(String),
    exchange Nullable(String),
    auditor_name Nullable(String),
    shares_outstanding Nullable(Decimal256(18)),
    shares_outstanding_date Nullable(Date),
    public_float Nullable(Decimal256(18)),
    security_members Array(String),
    security_titles Array(Nullable(String)),
    security_symbols Array(Nullable(String)),
    security_exchanges Array(Nullable(String)),
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision)
SETTINGS index_granularity = 8192;

-- One row per numeric fact whose dimensions are all on breakdown axes. `axis` and
-- `member_label` describe the first member (segment, product, geography,
-- consolidation); the member_* arrays list every member in that order.
CREATE TABLE IF NOT EXISTS {{database}}.xbrl_segment_facts
(
    accession String,
    cik UInt64,
    form LowCardinality(String),
    filed_date Date,
    fact_id FixedString(32),
    concept_qname String,
    axis LowCardinality(String),
    member_label String,
    member_axes Array(LowCardinality(String)),
    member_qnames Array(String),
    member_labels Array(String),
    value Decimal256(18),
    unit LowCardinality(Nullable(String)),
    period_start Nullable(Date),
    period_end Nullable(Date),
    period_instant Nullable(Date),
    is_elimination UInt8,
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, concept_qname, fact_id)
SETTINGS index_granularity = 8192;
