-- Stash runs (BINK-25): one append-only header per batch-routing pass.
--
-- A stash run fans out into ordinary bin_routing_requests receipts — the
-- batch is N single-item receipts sharing a stash_run_id, so the receipt
-- schema, idempotency handles, and decision ledger are untouched.
CREATE TABLE stash_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    seq BIGINT GENERATED ALWAYS AS IDENTITY,
    tenant_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL,
    external_id TEXT NOT NULL CHECK (btrim(external_id) <> ''),
    site TEXT NOT NULL CHECK (btrim(site) <> ''),
    source_kind TEXT NOT NULL DEFAULT 'text' CHECK (source_kind IN ('text', 'photo')),
    source_ref TEXT,
    item_count INTEGER NOT NULL CHECK (item_count >= 1),
    requested_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(raw_payload) = 'object')
);

CREATE UNIQUE INDEX stash_runs_scope_external_idx
    ON stash_runs (tenant_id, corpus_id, external_id);

CREATE TRIGGER stash_runs_append_only BEFORE UPDATE OR DELETE ON stash_runs
    FOR EACH ROW EXECUTE FUNCTION binkeeper_prevent_evidence_mutation();

GRANT SELECT ON stash_runs TO binkeeper_serving;
GRANT SELECT, INSERT ON stash_runs TO binkeeper_owner;

-- Receipts gain a nullable link back to their run. Additive DDL only; no
-- existing row is rewritten.
ALTER TABLE bin_routing_requests
    ADD COLUMN stash_run_id UUID REFERENCES stash_runs (id);
