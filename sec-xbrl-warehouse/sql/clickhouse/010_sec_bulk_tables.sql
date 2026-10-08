CREATE TABLE IF NOT EXISTS {{database}}.sec_submissions
(
    adsh String,
    cik UInt64,
    name String,
    sic Nullable(UInt16),
    countryba Nullable(FixedString(2)),
    stprba Nullable(String),
    cityba Nullable(String),
    zipba Nullable(String),
    bas1 Nullable(String),
    bas2 Nullable(String),
    baph Nullable(String),
    countryma Nullable(String),
    stprma Nullable(String),
    cityma Nullable(String),
    zipma Nullable(String),
    mas1 Nullable(String),
    mas2 Nullable(String),
    countryinc Nullable(FixedString(2)),
    stprinc Nullable(String),
    ein Nullable(String),
    former Nullable(String),
    changed Nullable(Date),
    afs LowCardinality(Nullable(String)),
    wksi UInt8,
    fye Nullable(FixedString(4)),
    form LowCardinality(String),
    period Nullable(Date),
    fy Nullable(Int16),
    fp LowCardinality(Nullable(String)),
    filed Date,
    accepted DateTime64(3, 'UTC'),
    prevrpt UInt8,
    detail UInt8,
    instance String,
    nciks UInt16,
    aciks Nullable(String),
    pubfloatusd Nullable(Decimal256(2)),
    floatdate Nullable(Date),
    floataxis Nullable(String),
    floatmems Nullable(UInt16),
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
PARTITION BY toYear(filed)
ORDER BY (adsh, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_tags
(
    tag String,
    version String,
    custom UInt8,
    abstract UInt8,
    datatype LowCardinality(Nullable(String)),
    iord LowCardinality(Nullable(String)),
    crdr LowCardinality(Nullable(String)),
    tlabel Nullable(String),
    doc Nullable(String),
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
ORDER BY (tag, version, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_dimensions
(
    dimh String,
    segments Nullable(String),
    segt UInt8,
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
ORDER BY (dimh, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_numeric_facts
(
    adsh String,
    cik UInt64,
    filed Date,
    form LowCardinality(String),
    fy Nullable(Int16),
    fp LowCardinality(Nullable(String)),
    tag String,
    version String,
    ddate String,
    qtrs Int16,
    uom LowCardinality(String),
    dimh String,
    iprx UInt16,
    value_raw Nullable(String),
    value_decimal Nullable(Decimal256(18)),
    footnote Nullable(String),
    footlen UInt32,
    dimn UInt8,
    coreg Nullable(String),
    durp Float32,
    datp Float32,
    dcml Int16,
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC'),
    INDEX idx_adsh adsh TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(_source_revision)
PARTITION BY toYear(filed)
ORDER BY (cik, tag, version, ddate, qtrs, uom, dimh, adsh, iprx, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_text_facts
(
    adsh String,
    cik UInt64,
    filed Date,
    form LowCardinality(String),
    fy Nullable(Int16),
    fp LowCardinality(Nullable(String)),
    tag String,
    version String,
    ddate String,
    qtrs Int16,
    iprx UInt16,
    lang LowCardinality(String),
    dcml Int16,
    durp Float32,
    datp Float32,
    dimh String,
    dimn Nullable(UInt8),
    coreg Nullable(String),
    escaped UInt8,
    srclen UInt32,
    txtlen Nullable(UInt32),
    footnote Nullable(String),
    footlen Nullable(UInt32),
    context String,
    value Nullable(String) CODEC(ZSTD(6)),
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC'),
    INDEX idx_adsh adsh TYPE bloom_filter GRANULARITY 1
)
ENGINE = ReplacingMergeTree(_source_revision)
PARTITION BY toYear(filed)
ORDER BY (cik, tag, version, ddate, qtrs, dimh, adsh, iprx, _artifact_id, _row_hash)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_renderings
(
    adsh String,
    report UInt32,
    rfile LowCardinality(String),
    menucat LowCardinality(Nullable(String)),
    shortname Nullable(String),
    longname String,
    roleuri String,
    parentroleuri Nullable(String),
    parentreport Nullable(UInt32),
    ultparentrpt Nullable(UInt32),
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
ORDER BY (adsh, report, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_presentations
(
    adsh String,
    report UInt32,
    line UInt32,
    stmt LowCardinality(Nullable(String)),
    inpth UInt8,
    tag String,
    version String,
    prole String,
    plabel Nullable(String),
    negating UInt8,
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
ORDER BY (adsh, report, line, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';

CREATE TABLE IF NOT EXISTS {{database}}.sec_calculations
(
    adsh String,
    grp UInt16,
    arc UInt16,
    negative Int16,
    ptag String,
    pversion String,
    ctag String,
    cversion String,
    _artifact_id UUID,
    _source_period LowCardinality(String),
    _source_revision UInt32,
    _row_hash FixedString(64),
    _ingested_at DateTime64(3, 'UTC')
)
ENGINE = ReplacingMergeTree(_source_revision)
ORDER BY (adsh, grp, arc, _artifact_id)
SETTINGS index_granularity = 8192, default_compression_codec = 'ZSTD(3)';
