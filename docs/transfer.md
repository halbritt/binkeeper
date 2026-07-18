# Evidence transfer

`binkeeper-transfer` moves only the accepted BinKeeper subset. Export opens an
explicit source URL, marks the transaction read-only before inspecting the
pinned source schema, and writes a new mode-0600 snapshot. It refuses to
overwrite an existing path. Import accepts only that snapshot and an explicit
target URL; it has no source connection or Engram dependency.

```sh
binkeeper-transfer export postgresql:///engram /tmp/binkeeper-transfer.json
binkeeper-transfer import /tmp/binkeeper-transfer.json \
  postgresql:///binkeeper-disposable
```

For the authority-cutover rehearsal, stage the snapshot's blobs before import:

```sh
binkeeper-transfer stage-blobs /tmp/binkeeper-transfer.json \
  ~/.config/engram/blob-vault.json \
  /etc/binkeeper/blob-vault.json \
  /tmp/binkeeper-transfer-staged.json
binkeeper-transfer import /tmp/binkeeper-transfer-staged.json \
  postgresql:///binkeeper-disposable
```

Staging reads the explicitly local Engram Garage or filesystem vault, verifies
the source ciphertext and plaintext hashes, and writes a new AES-256-GCM
envelope to the BinKeeper vault under a distinct key. The staged snapshot keeps
the complete authenticated source manifest and adds a migration receipt. Its
target manifest may differ only in each blob's storage and encryption envelope;
stable ids, plaintext hashes, sizes, content types, timestamps, provenance, and
every non-blob table must remain byte-for-byte equivalent as canonical JSON.

The manifest independently covers row counts, stable ids, full row payloads,
timestamps, blob hashes, location folds, trip load/arrival checksums, capture
passports, and immutable route receipts. Import verifies the source manifest
before writing, preserves ids and sequence order, reads the target back, and
rolls back if the resulting manifest differs. Re-running the same import is
idempotent.

The snapshot contains private owner evidence when used against the live source.
Keep it local, remove it after the cutover or rehearsal, and never commit it.
The export command copies blob metadata only. `stage-blobs` performs the local
read/re-encryption step for a rehearsal or an owner-approved cutover under
ADR 0002. It refuses equal source and target keys and public Garage endpoints.
