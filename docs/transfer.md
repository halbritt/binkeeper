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

The manifest independently covers row counts, stable ids, full row payloads,
timestamps, blob hashes, location folds, trip load/arrival checksums, capture
passports, and immutable route receipts. Import verifies the source manifest
before writing, preserves ids and sequence order, reads the target back, and
rolls back if the resulting manifest differs. Re-running the same import is
idempotent.

The snapshot contains private owner evidence when used against the live source.
Keep it local, remove it after the cutover or rehearsal, and never commit it.
This utility copies blob metadata only. Re-encryption of blob bytes remains an
owner-approved cutover action governed by ADR 0002.
