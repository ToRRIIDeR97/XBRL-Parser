CREATE VIEW IF NOT EXISTS {{database}}.v_sec_numeric_latest AS
SELECT *
FROM {{database}}.sec_numeric_facts FINAL;

CREATE VIEW IF NOT EXISTS {{database}}.v_xbrl_facts_latest AS
SELECT *
FROM {{database}}.xbrl_facts FINAL;

CREATE VIEW IF NOT EXISTS {{database}}.v_canonical_facts_latest AS
SELECT *
FROM {{database}}.canonical_facts FINAL;
