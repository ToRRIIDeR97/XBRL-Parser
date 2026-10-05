CREATE OR REPLACE VIEW {{database}}.v_sec_numeric_latest AS
SELECT *
FROM {{database}}.sec_numeric_facts FINAL
ORDER BY _source_period DESC, _source_revision DESC, _ingested_at DESC
LIMIT 1 BY adsh, tag, version, ddate, qtrs, uom, dimh, iprx;

CREATE OR REPLACE VIEW {{database}}.v_sec_text_latest AS
SELECT *
FROM {{database}}.sec_text_facts FINAL
ORDER BY _source_period DESC, _source_revision DESC, _ingested_at DESC
LIMIT 1 BY adsh, tag, version, ddate, qtrs, dimh, iprx;

CREATE OR REPLACE VIEW {{database}}.v_xbrl_facts_latest AS
SELECT * EXCEPT (raw_value_scale) REPLACE (
    lower(hex(fact_id)) AS fact_id,
    -- Tuple facts have no context: their blank hash is stored as zero bytes.
    if(context_hash = unhex(repeat('00', 32)), '', lower(hex(context_hash))) AS context_hash,
    lower(hex(dimension_hash)) AS dimension_hash,
    lower(hex(duplicate_set_hash)) AS duplicate_set_hash,
    arrayMap(x -> lower(hex(x)), footnote_ids) AS footnote_ids,
    lower(hex(_row_hash)) AS _row_hash,
    if(raw_value_scale IS NULL, raw_value, if(value_kind = 'numeric', if(raw_value_scale = 0, toString(value_decimal), substring(toDecimalString(value_decimal, 18), 1, length(toDecimalString(value_decimal, 18)) - 18 + raw_value_scale)), value_text)) AS raw_value
)
FROM {{database}}.xbrl_facts FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_canonical_facts_latest AS
SELECT * REPLACE (
    lower(hex(canonical_fact_id)) AS canonical_fact_id,
    lower(hex(raw_fact_id)) AS raw_fact_id,
    lower(hex(_row_hash)) AS _row_hash
)
FROM {{database}}.canonical_facts FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession)
ORDER BY mapping_version DESC, _ingested_at DESC
LIMIT 1 BY accession, raw_fact_id, canonical_metric;

CREATE OR REPLACE VIEW {{database}}.v_statement_lines_latest AS
SELECT * REPLACE (
    arrayMap(x -> lower(hex(x)), value_fact_ids) AS value_fact_ids,
    lower(hex(_row_hash)) AS _row_hash
)
FROM {{database}}.xbrl_statement_lines FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_cover_pages_latest AS
SELECT * REPLACE (
    lower(hex(_row_hash)) AS _row_hash
)
FROM {{database}}.xbrl_cover_pages FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_segment_facts_latest AS
SELECT * REPLACE (
    lower(hex(fact_id)) AS fact_id,
    lower(hex(_row_hash)) AS _row_hash
)
FROM {{database}}.xbrl_segment_facts FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

-- Footnote and policy text blocks as filed (HTML is not interpreted).
CREATE OR REPLACE VIEW {{database}}.v_text_blocks_latest AS
SELECT
    accession,
    cik,
    company_name,
    form,
    filed_date,
    fiscal_year,
    fiscal_period,
    fact_id,
    concept_namespace,
    concept_name,
    concept_qname,
    endsWith(concept_name, 'PolicyTextBlock') AS is_policy,
    period_start,
    period_end,
    period_instant,
    language,
    dimensions_json,
    value_text,
    source_document,
    extraction_revision
FROM {{database}}.v_xbrl_facts_latest
WHERE endsWith(concept_name, 'TextBlock');

-- Per filing: its extension concepts plus the standard taxonomy concepts its facts use.
-- A standard concept's labels are the filing's own (xbrl_concept_labels) when it labels
-- the concept itself, else the taxonomy-owned labels.
CREATE OR REPLACE VIEW {{database}}.v_filing_concepts_latest AS
SELECT
    accession, concept_namespace, concept_name, concept_qname, taxonomy_version,
    datatype_qname, substitution_group_qname, period_type, balance, abstract, nillable,
    is_extension, labels_json, documentation, references_json
FROM {{database}}.xbrl_concepts FINAL
WHERE is_extension = 1
  AND (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession)
UNION ALL
SELECT
    used.accession, t.concept_namespace, t.concept_name, t.concept_qname, t.taxonomy_version,
    t.datatype_qname, t.substitution_group_qname, t.period_type, t.balance, t.abstract,
    t.nillable, toUInt8(0) AS is_extension,
    if(empty(own.labels_json), t.labels_json, own.labels_json) AS labels_json,
    t.documentation, t.references_json
FROM (
    SELECT DISTINCT accession, concept_namespace, concept_name
    FROM {{database}}.v_xbrl_facts_latest
) AS used
INNER JOIN (
    -- The namespace URI names the taxonomy year; one row per concept.
    SELECT *
    FROM {{database}}.taxonomy_concepts FINAL
    ORDER BY ifNull(taxonomy_version, '') DESC
    LIMIT 1 BY concept_namespace, concept_name
) AS t
    ON t.concept_namespace = used.concept_namespace
   AND t.concept_name = used.concept_name
LEFT JOIN (
    SELECT accession, concept_namespace, concept_name, labels_json
    FROM {{database}}.xbrl_concept_labels FINAL
    WHERE (accession, extraction_revision) IN
        (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession)
) AS own
    ON own.accession = used.accession
   AND own.concept_namespace = used.concept_namespace
   AND own.concept_name = used.concept_name;
