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
SELECT *
FROM {{database}}.xbrl_facts FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_canonical_facts_latest AS
SELECT *
FROM {{database}}.canonical_facts FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession)
ORDER BY mapping_version DESC, _ingested_at DESC
LIMIT 1 BY accession, raw_fact_id, canonical_metric;

CREATE OR REPLACE VIEW {{database}}.v_statement_lines_latest AS
SELECT *
FROM {{database}}.xbrl_statement_lines FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_cover_pages_latest AS
SELECT *
FROM {{database}}.xbrl_cover_pages FINAL
WHERE (accession, extraction_revision) IN
    (SELECT accession, max(extraction_revision) FROM {{database}}.xbrl_extractions GROUP BY accession);

CREATE OR REPLACE VIEW {{database}}.v_segment_facts_latest AS
SELECT *
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
