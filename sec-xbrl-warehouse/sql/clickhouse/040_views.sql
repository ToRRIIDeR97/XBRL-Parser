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
