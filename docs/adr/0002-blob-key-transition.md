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

This slice implements and tests the destination path with synthetic bytes and
a disposable key. It does not read Engram blobs, reuse its key, or establish
production key custody.

## Consequences

Cutover must prove source and destination plaintext hashes match before the new
blob is accepted. Rollback retains Engram's unchanged blob and key authority.
The temporary plaintext boundary exists only inside the later owner-approved,
local import process and must not be written to disk.
