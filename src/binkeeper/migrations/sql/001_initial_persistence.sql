CREATE EXTENSION IF NOT EXISTS pgcrypto;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'binkeeper_owner') THEN
        CREATE ROLE binkeeper_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'binkeeper_serving') THEN
        CREATE ROLE binkeeper_serving NOLOGIN;
    END IF;
END
$$;

GRANT binkeeper_owner TO CURRENT_USER;
GRANT binkeeper_serving TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO binkeeper_owner, binkeeper_serving;

CREATE OR REPLACE FUNCTION binkeeper_prevent_evidence_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; % is not allowed', TG_TABLE_NAME, TG_OP
        USING ERRCODE = 'P0001';
END;
$$;

CREATE TABLE capture_evidence (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE CHECK (btrim(external_id) <> ''),
    source_external_id TEXT,
    captured_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    content_text TEXT,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    privacy_class TEXT NOT NULL CHECK (btrim(privacy_class) <> ''),
    provenance JSONB NOT NULL CHECK (jsonb_typeof(provenance) = 'object')
);

CREATE TABLE bin_trip_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE CHECK (btrim(external_id) <> ''),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('place','open','load','arrive','close','confirm','contradict')),
    trip_id TEXT, bin_code TEXT, from_site TEXT, to_site TEXT, site TEXT,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(provenance) = 'object'),
    CHECK ((event_kind IN ('open','close')) = (bin_code IS NULL)),
    CHECK (event_kind IN ('place','confirm','contradict') OR trip_id IS NOT NULL),
    CHECK (event_kind NOT IN ('place','arrive','confirm') OR site IS NOT NULL)
);

CREATE TABLE location_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, bin_code TEXT NOT NULL, observed_site TEXT NOT NULL,
    source_kind TEXT NOT NULL, observation_strength DOUBLE PRECISION NOT NULL DEFAULT 1.0
        CHECK (observation_strength BETWEEN 0.0 AND 1.0),
    evidence_ref TEXT, observed_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb, privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE bin_presence_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, event_kind TEXT NOT NULL CHECK (event_kind IN ('arrive','dwell','depart')),
    site TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    accuracy_m DOUBLE PRECISION CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb, privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE bin_resting_order_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, event_kind TEXT NOT NULL CHECK (event_kind IN ('create','clear')),
    order_id TEXT NOT NULL, site TEXT, bin_code TEXT, action_kind TEXT, instruction TEXT,
    cleared_reason TEXT, priority DOUBLE PRECISION, occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(), payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    privacy_class TEXT NOT NULL DEFAULT 'private', provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    CHECK ((event_kind = 'create' AND site IS NOT NULL AND action_kind IS NOT NULL AND instruction IS NOT NULL)
        OR (event_kind = 'clear' AND action_kind IS NULL AND instruction IS NULL AND priority IS NULL))
);

CREATE TABLE bin_routing_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, input_text TEXT NOT NULL, site TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    router_version TEXT NOT NULL, item_card JSONB NOT NULL, route_result JSONB NOT NULL,
    route_result_sha256 TEXT NOT NULL CHECK (route_result_sha256 ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb, privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE bin_placement_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, routing_request_id UUID NOT NULL REFERENCES bin_routing_requests(id),
    decision_kind TEXT NOT NULL CHECK (decision_kind IN ('accept','reject','override','split','merge','not_an_item','create_new_bin')),
    recommended_bin_code TEXT, selected_bin_code TEXT, actor TEXT NOT NULL DEFAULT 'owner',
    decided_at TIMESTAMPTZ NOT NULL, recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reason TEXT, payload JSONB NOT NULL DEFAULT '{}'::jsonb, privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE bin_item_liveness (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    external_id TEXT NOT NULL UNIQUE, phrase_norm TEXT NOT NULL, item_phrase TEXT NOT NULL,
    source_kind TEXT NOT NULL, source_ref TEXT NOT NULL, mentioned_at TIMESTAMPTZ NOT NULL,
    harvested_at TIMESTAMPTZ NOT NULL DEFAULT now(), harvester_version TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb, privacy_class TEXT NOT NULL DEFAULT 'private',
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (phrase_norm, source_kind, source_ref)
);

CREATE TABLE evidence_blobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(), seq BIGINT GENERATED ALWAYS AS IDENTITY,
    plaintext_sha256 TEXT NOT NULL UNIQUE CHECK (plaintext_sha256 ~ '^[0-9a-f]{64}$'),
    byte_size BIGINT NOT NULL CHECK (byte_size >= 0), content_type TEXT,
    storage_backend TEXT NOT NULL, object_key TEXT NOT NULL,
    encryption_algorithm TEXT NOT NULL, ciphertext_sha256 TEXT NOT NULL CHECK (ciphertext_sha256 ~ '^[0-9a-f]{64}$'),
    nonce BYTEA NOT NULL, key_ref TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    privacy_class TEXT NOT NULL DEFAULT 'private', provenance JSONB NOT NULL DEFAULT '{}'::jsonb
);

DO $$
DECLARE relation_name TEXT;
BEGIN
    FOREACH relation_name IN ARRAY ARRAY[
        'capture_evidence','bin_trip_events','location_observations','bin_presence_events',
        'bin_resting_order_events','bin_routing_requests','bin_placement_decisions',
        'bin_item_liveness','evidence_blobs'
    ] LOOP
        EXECUTE format(
            'CREATE TRIGGER %I_append_only BEFORE UPDATE OR DELETE ON %I '
            'FOR EACH ROW EXECUTE FUNCTION binkeeper_prevent_evidence_mutation()',
            relation_name, relation_name
        );
        EXECUTE format('GRANT SELECT, INSERT ON %I TO binkeeper_owner', relation_name);
        EXECUTE format('GRANT SELECT ON %I TO binkeeper_serving', relation_name);
        EXECUTE format('REVOKE INSERT, UPDATE, DELETE ON %I FROM binkeeper_serving', relation_name);
    END LOOP;
END
$$;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO binkeeper_owner;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM binkeeper_serving;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT ON TABLES TO binkeeper_owner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO binkeeper_owner;
