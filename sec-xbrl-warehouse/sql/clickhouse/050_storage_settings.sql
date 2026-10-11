-- Brings tables created before these settings existed up to the CREATE TABLE
-- definitions above. Every statement is idempotent.
--
-- ZSTD(3) applies to new parts; existing parts are recompressed as they merge
-- (or by OPTIMIZE TABLE ... FINAL). The bloom-filter indexes let accession
-- lookups skip granules on tables sorted by CIK. New parts get them
-- automatically; build them for existing parts once with
-- ALTER TABLE ... MATERIALIZE INDEX (OPERATIONS.md).

ALTER TABLE {{database}}.sec_submissions MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_tags MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_dimensions MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_numeric_facts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_text_facts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_renderings MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_presentations MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.sec_calculations MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_contexts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_concepts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_facts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_relationships MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_footnotes MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.taxonomy_concepts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_concept_labels MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_roles MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_statement_lines MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_cover_pages MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.xbrl_segment_facts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.canonical_facts MODIFY SETTING default_compression_codec = 'ZSTD(3)';
ALTER TABLE {{database}}.metric_series MODIFY SETTING default_compression_codec = 'ZSTD(3)';

ALTER TABLE {{database}}.xbrl_facts ADD INDEX IF NOT EXISTS idx_accession accession TYPE bloom_filter GRANULARITY 1;
ALTER TABLE {{database}}.canonical_facts ADD INDEX IF NOT EXISTS idx_accession accession TYPE bloom_filter GRANULARITY 1;
ALTER TABLE {{database}}.sec_numeric_facts ADD INDEX IF NOT EXISTS idx_adsh adsh TYPE bloom_filter GRANULARITY 1;
ALTER TABLE {{database}}.sec_text_facts ADD INDEX IF NOT EXISTS idx_adsh adsh TYPE bloom_filter GRANULARITY 1;
