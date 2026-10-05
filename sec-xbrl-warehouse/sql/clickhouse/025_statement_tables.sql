-- Role types and primary financial statements as presented (derived from the
-- extraction's role definitions, presentation arcs, and facts; rebuildable from silver).

CREATE TABLE IF NOT EXISTS {{database}}.xbrl_roles
(
    accession String,
    role_uri String,
    definition Nullable(String),
    used_on Array(String),
    sort_code Nullable(String),
    category LowCardinality(String),
    statement_kind LowCardinality(Nullable(String)),
    extraction_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, role_uri)
SETTINGS index_granularity = 8192;

-- One row per statement line; the line's values are parallel arrays (one entry per
-- period, or per concept and period on `member` lines). Read lines with
-- ORDER BY role_uri, line_number.
CREATE TABLE IF NOT EXISTS {{database}}.xbrl_statement_lines
(
    accession String,
    cik UInt64,
    form LowCardinality(String),
    filed_date Date,
    role_uri String,
    statement_kind LowCardinality(String),
    line_number UInt32,
    depth UInt16,
    concept_qname String,
    parent_concept_qname Nullable(String),
    label String,
    preferred_label_role Nullable(String),
    is_abstract UInt8,
    is_total UInt8,
    line_role LowCardinality(String),
    value_fact_ids Array(String),
    value_concept_qnames Array(String),
    value_period_starts Array(Nullable(Date)),
    value_period_ends Array(Nullable(Date)),
    value_period_instants Array(Nullable(Date)),
    value_decimals Array(Nullable(Decimal256(18))),
    extraction_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, role_uri, line_number)
SETTINGS index_granularity = 8192;
