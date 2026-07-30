-- Physical bin-in-bin containment (BINK-38).
--
-- Pack and unpack are immutable owner attestations. The current single-parent
-- graph and effective locations are rebuilt from this ledger; no editable
-- parent column is introduced.
CREATE TABLE bin_containment_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    external_id TEXT NOT NULL CHECK (btrim(external_id) <> ''),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('pack', 'unpack')),
    bin_code TEXT NOT NULL CHECK (btrim(bin_code) <> ''),
    container_code TEXT NOT NULL CHECK (btrim(container_code) <> ''),
    site TEXT NOT NULL CHECK (btrim(site) <> ''),
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_label TEXT NOT NULL DEFAULT 'manual' CHECK (btrim(source_label) <> ''),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(raw_payload) = 'object'),
    CHECK (bin_code <> container_code)
);

CREATE UNIQUE INDEX bin_containment_events_scope_external_idx
    ON bin_containment_events (tenant_id, corpus_id, external_id);
CREATE INDEX bin_containment_events_bin_idx
    ON bin_containment_events (tenant_id, corpus_id, bin_code, seq);
CREATE INDEX bin_containment_events_container_idx
    ON bin_containment_events (tenant_id, corpus_id, container_code, seq);

CREATE TRIGGER bin_containment_events_append_only
    BEFORE UPDATE OR DELETE ON bin_containment_events
    FOR EACH ROW EXECUTE FUNCTION binkeeper_prevent_evidence_mutation();

GRANT SELECT ON bin_containment_events TO binkeeper_serving;
GRANT SELECT, INSERT ON bin_containment_events TO binkeeper_owner;
