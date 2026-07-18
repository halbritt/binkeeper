# ADR 0002: Re-encrypt imported blobs under a BinKeeper-owned key

- Status: accepted
- Date: 2026-07-18

## Context

BinKeeper must own its blob authority after cutover without copying Engram key
custody into the new service. Blob bytes remain local and ciphertext-only in
the content-addressed store.

## Decision

An approved import will decrypt each source blob through Engram's existing
local authority, verify its plaintext hash, and immediately encrypt it with a
new BinKeeper-owned AES-256-GCM key. BinKeeper records the new ciphertext hash,
nonce, and opaque key reference. It never stores key material in PostgreSQL or
Git.

The cutover transfer now implements this path with separate source and target
manifests. Synthetic tests cover its failure boundaries; BINK-11 may exercise
it against a current read-only local snapshot and a disposable target key. It
does not reuse the Engram key, establish production key custody, or authorize
the production writer switch.

## Consequences

Cutover must prove source and destination plaintext hashes match before the new
blob is accepted. Rollback retains Engram's unchanged blob and key authority.
The temporary plaintext boundary exists only inside the later owner-approved,
local import process and must not be written to disk.
