-- Row counts + content fingerprints used to verify a migration or restore.
-- Critical (non-re-creatable) tables are fingerprinted, not just counted.
SELECT * FROM (
SELECT 'corpus_source', count(*)::text, md5(string_agg(logical_key || role, ',' ORDER BY logical_key)) FROM corpus_source
UNION ALL SELECT 'segment', count(*)::text, md5(string_agg(coalesce(sha256, ''), ',' ORDER BY id)) FROM segment
UNION ALL SELECT 'model', count(*)::text, md5(string_agg(logical_name || coalesce(model_sha256, ''), ',' ORDER BY logical_name)) FROM model
UNION ALL SELECT 'model_suite', count(*)::text, '' FROM model_suite
UNION ALL SELECT 'sweep_run', count(*)::text, md5(string_agg(config_sha256, ',' ORDER BY id)) FROM sweep_run
UNION ALL SELECT 'transcription_result', count(*)::text, md5(string_agg(id::text || coalesce(artifact_sha256, ''), ',' ORDER BY id)) FROM transcription_result
UNION ALL SELECT 'reference.gold_segment', count(*)::text, md5(string_agg(text_raw, ',' ORDER BY id)) FROM reference.gold_segment
UNION ALL SELECT 'annotation_thread', count(*)::text, '' FROM annotation_thread
UNION ALL SELECT 'annotation_version', count(*)::text, md5(string_agg(coalesce(text, '') || training_label || review_status, ',' ORDER BY id)) FROM annotation_version
UNION ALL SELECT 'training_dataset', count(*)::text, md5(string_agg(manifest_sha256, ',' ORDER BY id)) FROM training_dataset
UNION ALL SELECT 'airport', count(*)::text, '' FROM airport
UNION ALL SELECT 'context_snapshot', count(*)::text, md5(string_agg(response_sha256, ',' ORDER BY id)) FROM context_snapshot
UNION ALL SELECT 'segment_agreement', count(*)::text, '' FROM segment_agreement
) AS verified ORDER BY 1;
