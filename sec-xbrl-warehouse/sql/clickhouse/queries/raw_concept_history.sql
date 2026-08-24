SELECT
    cik,
    accession,
    filed_date,
    concept_qname,
    period_start,
    period_end,
    period_instant,
    raw_value,
    value_decimal,
    dimensions_json,
    decimals,
    source_document
FROM {{database}}.v_xbrl_facts_latest
WHERE cik = {cik:UInt64}
  AND concept_namespace = {namespace:String}
  AND concept_name = {concept:String}
ORDER BY coalesce(period_end, period_instant), filed_date;
