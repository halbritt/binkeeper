# One-writer cutover and rollback runbook

Status: executed 2026-07-18 in the owner-approved `BINK-11` window (receipt
below). The sections that follow are the preserved execution record, not a
standing instruction: the Engram rollback path they reference expired with the
`BINK-13` retirement, and any future writer transition requires a new
owner-accepted work item.

This is the execution procedure for BINK-11. It did not itself authorize a
production cutover.

## Mandatory gate

Stop before the live freeze until an owner-approved Plane comment on BINK-11
names the UTC window, executing operator, rollback owner, Engram and BinKeeper
SHAs, current manifest identity, service endpoint, and maximum verification
window. A green test suite, fresh backup, or successful rehearsal is not that
approval.

## Preflight

1. Confirm Engram `:8765` is healthy and remains installed as rollback writer.
2. Confirm BinKeeper code, migrations, service unit, backup timer, and restore
   timer match the accepted SHAs; `/readyz` must be green on the rehearsal DB.
3. Run a fresh authenticated backup and disposable restore drill.
4. Run the read-only Engram exporter, then `binkeeper-transfer stage-blobs`
   with distinct Engram and BinKeeper keys. Compare all 8 captures, 5 move
   events, 4 blobs, latest projections, trip checksums, passports, and route
   receipts to the source and staged manifests. Decrypt every staged blob and
   verify its original plaintext hash. Counts are a floor, not a substitute
   for hashes.
5. Keep `BINKEEPER_WRITES_ENABLED=0` and prove a POST returns HTTP 503 and a
   CLI/MCP mutation refuses before dry-running catalog, manage, media, reads,
   and one non-committing physical action path through tailnet HTTPS `:8766`.

Stop after any stale backup, restore mismatch, manifest drift, origin failure,
unavailable media, projection difference, unaccounted trip, or unclear rollback
owner.

## Approved live window

1. Freeze every Engram BinKeeper writer. Prove a write attempt is explicitly
   refused; do not rely on operator intent alone.
2. Record the final Engram evidence watermark and run the pinned read-only
   export. Import once into the empty migrated BinKeeper authority and rerun the
   import to prove idempotency.
3. Compare the full manifest and decrypt all four blob copies under the
   BinKeeper-owned key. Do not delete or rewrite the Engram originals.
4. Start BinKeeper on loopback `127.0.0.1:8766` with writes still disabled;
   verify local and tailnet
   `/healthz`, `/readyz`, catalog, authoring, media, CLI, and MCP.
5. Configure the dormant Engram compatibility variables to the absolute
   BinKeeper CLI and exact `https://proximal.tail0ecc2e.ts.net:8766` origin,
   restart Engram, and prove legacy names reach only BinKeeper. Engram remains
   on `:8765`; the existing `:8766` tailnet mapping is not recreated.
6. Set `BINKEEPER_WRITES_ENABLED=1` and restart BinKeeper only after the old
   writer refusal and all read-path comparisons are recorded. Prove the old
   writer is still refused, then record the first BinKeeper evidence watermark.

There is no dual-write step.

## Immediate rollback

Rollback is mandatory for a manifest mismatch, incorrect protected projection,
failed physical-action path, failed backup/restore check, lost tailnet access,
or evidence that both writers can accept a request.

1. Stop the BinKeeper writer and preserve its database, blob root, logs, first
   watermark, and every artifact. Do not merge its new evidence into Engram
   during the incident.
2. Remove the Engram compatibility environment, restart Engram on its unchanged
   database, and prove the Engram writer is the only accepting writer.
3. Verify `:8765`, CLI/MCP, catalog, authoring, media, trip folds, and the final
   pre-freeze Engram watermark.
4. Record the failure and exact rollback boundary in BINK-11. Do not retry the
   cutover without a new owner-approved window.

## 2026-07-18 execution receipt

The owner-approved window ran from `2026-07-18T17:29:37Z` through
`2026-07-18T17:59:37Z`. Engram SHA
`77da129138d75522d787bfbfb9bdd003566bd6cf` was frozen before final export;
BinKeeper SHA `eceb12534f9ed7d2892a3be0bbd0c1786fd9d4a6` became the sole writer.
The final source manifest was
`08c8a07d9b445d2f03d46c3806a868fedbf470dacaf8c1bd67f5eb697e146988`;
the re-encrypted target manifest was
`6e0cd78c5204d20f10e2b6dc8a4dbe8d64ad9e7a0798d4fd409536a05721c65c`.
Import and idempotent replay preserved 8 captures, 5 move events, 4 blobs, and
move watermark 5. Foreground backup, disposable restore, local and tailnet
health/readiness, catalog, authoring, management, media, CLI, MCP, legacy
redirects, and old-writer refusal all passed. Plane `BINK-11` contains the
approval and detailed execution receipts.
