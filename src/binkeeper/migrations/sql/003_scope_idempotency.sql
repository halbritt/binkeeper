-- Replace bootstrap-era global uniqueness with the accepted tenant/corpus scope.
ALTER TABLE capture_evidence DROP CONSTRAINT IF EXISTS capture_evidence_external_id_key;
ALTER TABLE bin_trip_events DROP CONSTRAINT IF EXISTS bin_trip_events_external_id_key;
ALTER TABLE location_observations DROP CONSTRAINT IF EXISTS location_observations_external_id_key;
ALTER TABLE bin_presence_events DROP CONSTRAINT IF EXISTS bin_presence_events_external_id_key;
ALTER TABLE bin_resting_order_events
    DROP CONSTRAINT IF EXISTS bin_resting_order_events_external_id_key;
ALTER TABLE bin_routing_requests DROP CONSTRAINT IF EXISTS bin_routing_requests_external_id_key;
ALTER TABLE bin_placement_decisions
    DROP CONSTRAINT IF EXISTS bin_placement_decisions_external_id_key;
ALTER TABLE bin_item_liveness DROP CONSTRAINT IF EXISTS bin_item_liveness_external_id_key;
ALTER TABLE bin_item_liveness
    DROP CONSTRAINT IF EXISTS bin_item_liveness_phrase_norm_source_kind_source_ref_key;
ALTER TABLE evidence_blobs DROP CONSTRAINT IF EXISTS evidence_blobs_plaintext_sha256_key;

CREATE UNIQUE INDEX capture_evidence_scope_external_idx
    ON capture_evidence (tenant_id, corpus_id, external_id);
