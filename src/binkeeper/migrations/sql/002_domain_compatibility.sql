DO $$
DECLARE relation_name TEXT;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'capture_evidence','bin_trip_events','location_observations','bin_presence_events',
        'bin_resting_order_events','bin_routing_requests','bin_placement_decisions',
        'bin_item_liveness','evidence_blobs'
    ] LOOP
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''personal''',
            relation_name
        );
        EXECUTE format(
            'ALTER TABLE %I ADD COLUMN corpus_id TEXT NOT NULL DEFAULT ''personal''',
            relation_name
        );
    END LOOP;
END
$$;

CREATE UNIQUE INDEX bin_trip_events_scope_external_idx
    ON bin_trip_events (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX location_observations_scope_external_idx
    ON location_observations (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX bin_presence_events_scope_external_idx
    ON bin_presence_events (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX bin_resting_order_events_scope_external_idx
    ON bin_resting_order_events (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX bin_routing_requests_scope_external_idx
    ON bin_routing_requests (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX bin_placement_decisions_scope_external_idx
    ON bin_placement_decisions (tenant_id, corpus_id, external_id);
CREATE UNIQUE INDEX bin_item_liveness_scope_source_idx
    ON bin_item_liveness (tenant_id, corpus_id, phrase_norm, source_kind, source_ref);
CREATE UNIQUE INDEX evidence_blobs_scope_plaintext_idx
    ON evidence_blobs (tenant_id, corpus_id, plaintext_sha256);

ALTER TABLE bin_trip_events RENAME COLUMN payload TO raw_payload;
ALTER TABLE bin_trip_events ADD COLUMN source_label TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE bin_presence_events RENAME COLUMN payload TO raw_payload;
ALTER TABLE bin_presence_events ADD COLUMN source_label TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE bin_resting_order_events RENAME COLUMN payload TO raw_payload;
ALTER TABLE bin_resting_order_events ADD COLUMN source_label TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE bin_item_liveness RENAME COLUMN payload TO raw_payload;
ALTER TABLE bin_item_liveness ALTER COLUMN external_id SET DEFAULT gen_random_uuid()::text;
ALTER TABLE bin_routing_requests RENAME COLUMN item_card TO item_card_json;
ALTER TABLE bin_routing_requests RENAME COLUMN route_result TO route_result_json;
ALTER TABLE bin_routing_requests RENAME COLUMN payload TO raw_payload;
ALTER TABLE bin_routing_requests ADD COLUMN request_kind TEXT NOT NULL DEFAULT 'text';
ALTER TABLE bin_routing_requests ADD COLUMN source_label TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE bin_placement_decisions RENAME COLUMN payload TO decision_payload;
ALTER TABLE bin_placement_decisions ADD COLUMN raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE capture_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_kind TEXT NOT NULL,
    external_id TEXT NOT NULL UNIQUE,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE TRIGGER capture_sources_append_only
    BEFORE UPDATE OR DELETE ON capture_sources
    FOR EACH ROW EXECUTE FUNCTION binkeeper_prevent_evidence_mutation();
CREATE VIEW sources AS SELECT id, source_kind, external_id, raw_payload FROM capture_sources;

CREATE VIEW location_observation AS
SELECT id, seq, tenant_id, corpus_id, external_id, bin_code, observed_site,
       source_kind, observation_strength, evidence_ref, observed_at, recorded_at,
       payload AS raw_payload
FROM location_observations;

CREATE VIEW captures AS
SELECT id,
       CASE WHEN source_external_id ~ '^[0-9a-f-]{36}$' THEN source_external_id::uuid END AS source_id,
       COALESCE(provenance->>'source_kind', 'capture') AS source_kind,
       external_id,
       recorded_at AS imported_at,
       payload AS raw_payload,
       COALESCE(NULLIF(regexp_replace(privacy_class, '[^0-9]', '', 'g'), '')::int, 1) AS privacy_tier,
       COALESCE(payload->>'capture_type', 'observation') AS capture_type,
       NULL::uuid AS corrects_belief_id,
       content_text,
       captured_at AS observed_at,
       tenant_id,
       corpus_id,
       NULL::uuid AS bundle_id
FROM capture_evidence;

CREATE OR REPLACE FUNCTION binkeeper_insert_capture_compatibility()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO capture_evidence (
        id, external_id, source_external_id, captured_at, recorded_at,
        content_text, payload, privacy_class, provenance, tenant_id, corpus_id
    ) VALUES (
        COALESCE(NEW.id, gen_random_uuid()),
        NEW.external_id,
        NEW.source_id::text,
        COALESCE(NEW.observed_at, NEW.imported_at, now()),
        COALESCE(NEW.imported_at, now()),
        NEW.content_text,
        COALESCE(NEW.raw_payload, '{}'::jsonb),
        'tier-' || COALESCE(NEW.privacy_tier, 1)::text,
        jsonb_build_object(
            'source_kind', COALESCE(NEW.source_kind, 'capture'),
            'tenant_id', COALESCE(NEW.tenant_id, 'personal'),
            'corpus_id', COALESCE(NEW.corpus_id, 'personal')
        ),
        COALESCE(NEW.tenant_id, 'personal'),
        COALESCE(NEW.corpus_id, 'personal')
    );
    RETURN NEW;
END;
$$;

CREATE TRIGGER captures_insert
    INSTEAD OF INSERT ON captures
    FOR EACH ROW EXECUTE FUNCTION binkeeper_insert_capture_compatibility();

GRANT SELECT ON captures, location_observation TO binkeeper_serving;
GRANT SELECT ON capture_sources TO binkeeper_owner;
