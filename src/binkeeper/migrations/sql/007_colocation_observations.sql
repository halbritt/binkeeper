-- Co-location observations (BINK-31): witnessed anchor↔bin adjacency.
--
-- One immutable row per (anchor, member) pair seen together in one photo.
-- Advisory by construction: rows corroborate a containment fold (BINK-32)
-- and never move a bin. Strength is geometry-weighted at append time —
-- ambiguous geometry records low strength rather than confident error.
CREATE TABLE colocation_observations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    external_id TEXT NOT NULL CHECK (btrim(external_id) <> ''),
    anchor_code TEXT NOT NULL CHECK (anchor_code LIKE 'LOC-%'),
    member_code TEXT NOT NULL CHECK (btrim(member_code) <> ''),
    observed_at TIMESTAMPTZ NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'qr_colocation'
        CHECK (source_kind IN ('qr_colocation', 'ocr_anchor')),
    observation_strength DOUBLE PRECISION NOT NULL
        CHECK (observation_strength BETWEEN 0.0 AND 1.0),
    evidence_ref TEXT,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_payload) = 'object')
);

CREATE UNIQUE INDEX colocation_observations_scope_external_idx
    ON colocation_observations (tenant_id, corpus_id, external_id);
CREATE INDEX colocation_observations_member_idx
    ON colocation_observations (tenant_id, corpus_id, member_code);

CREATE TRIGGER colocation_observations_append_only
    BEFORE UPDATE OR DELETE ON colocation_observations
    FOR EACH ROW EXECUTE FUNCTION binkeeper_prevent_evidence_mutation();

GRANT SELECT ON colocation_observations TO binkeeper_serving;
GRANT SELECT, INSERT ON colocation_observations TO binkeeper_owner;
