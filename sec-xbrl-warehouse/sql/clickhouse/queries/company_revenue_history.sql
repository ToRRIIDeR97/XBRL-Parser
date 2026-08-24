SELECT
    cik,
    accession,
    filed_date,
    period_start,
    period_end,
    normalized_value_decimal AS revenue,
    normalized_unit,
    dimensions_json,
    mapping_confidence
FROM {{database}}.v_canonical_facts_latest
WHERE cik = {cik:UInt64}
  AND canonical_metric = 'revenue'
  AND period_end >= {start_date:Date}
ORDER BY period_end, filed_date;
