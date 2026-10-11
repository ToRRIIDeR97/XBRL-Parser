-- Standard taxonomy concepts (us-gaap, dei, srt, ...) stored once per concept and
-- taxonomy version instead of once per filing; xbrl_concepts keeps each filing's
-- extension concepts.
--
-- Labels: SEC filings don't load the standard label linkbases, so a standard concept's
-- labels mostly come from the filer's own label linkbase. A concept the filing labels
-- itself (a label arc in one of the filing's documents) gets labels_json '[]' and
-- labels_rank 0 here, and its labels go to xbrl_concept_labels for that filing only;
-- otherwise the labels are taxonomy-owned (labels_rank 1). FINAL keeps the highest rank.

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
    labels_rank UInt8,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(labels_rank)
ORDER BY (concept_namespace, concept_name, ifNull(taxonomy_version, ''))
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

-- A filing's labels for standard concepts it labels itself: the concept's full label
-- set as the filing's DTS defines it (filer labels plus any it inherits).
CREATE TABLE IF NOT EXISTS {{database}}.xbrl_concept_labels
(
    accession String,
    concept_namespace String,
    concept_name String,
    concept_qname String,
    labels_json String CODEC(ZSTD(6)),
    extraction_revision UInt32,
    _row_hash FixedString(32),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(extraction_revision)
ORDER BY (accession, extraction_revision, concept_namespace, concept_name)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';
