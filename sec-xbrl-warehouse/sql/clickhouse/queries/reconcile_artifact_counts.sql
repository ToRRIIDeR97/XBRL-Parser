SELECT 'sub' AS table_name, count() AS rows
FROM {{database}}.sec_submissions
WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'tag', count() FROM {{database}}.sec_tags WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'dim', count() FROM {{database}}.sec_dimensions WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'num', count() FROM {{database}}.sec_numeric_facts WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'txt', count() FROM {{database}}.sec_text_facts WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'ren', count() FROM {{database}}.sec_renderings WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'pre', count() FROM {{database}}.sec_presentations WHERE _artifact_id = {artifact_id:UUID}
UNION ALL
SELECT 'cal', count() FROM {{database}}.sec_calculations WHERE _artifact_id = {artifact_id:UUID};
