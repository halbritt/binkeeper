---
status: accepted
date: 2026-07-18
plane: BINK-2
---

# Make BinKeeper the standalone physical-inventory authority

BinKeeper will become the sole writer and authority for physical-inventory
evidence after the verified cutover in `BINK-11`. Until that cutover succeeds,
Engram remains the sole writer and runtime authority; there is no dual-write
interval. The evidence for the extraction and the complete preservation matrix
live in the [extraction analysis](../extraction-analysis.md), and owner
acceptance and operating defaults are recorded in Plane `BINK-2`.

Implementation status: `BINK-11` completed in an owner-approved window on
2026-07-18. BinKeeper is now the sole physical-inventory writer and authority;
Engram's direct inventory writer is frozen while the separately gated
compatibility and retirement work proceeds.

BinKeeper owns its Python package, database and migrations, local process, CLI,
web surface, and `binkeeper.*` MCP interface. It initially uses the existing
local PostgreSQL cluster through dedicated BinKeeper database roles. Its public
domain interface covers locating and attesting bins, recording moves and
trips, inventory search, passport projection, placement recommendations and
receipts, and managed photos, profiles, and label intents. Callers do not
depend on storage tables or implementation modules.

## Operating defaults

- Engram compatibility shims remain until `BINK-12` consumer probes pass, with
  a hard maximum of 30 days after production cutover. Extension requires a new
  owner decision.
- During the verification window, the owner or executing operator may restore
  the Engram writer immediately after a manifest mismatch, incorrect protected
  projection, failed physical-action path, or failed backup/restore check.
- The owner surface stays local and is published through tailnet HTTPS on
  `proximal.tail0ecc2e.ts.net`. Engram's active `:8765` route stays in place
  throughout the compatibility window. `BINK-9` must freeze a dedicated,
  collision-free BinKeeper HTTPS and loopback port before changing routes.
- The four current bin-linked blobs are re-encrypted under a BinKeeper-owned
  key. Migration and restored backups must reproduce their plaintext hashes.
  This decision does not authorize a live key or blob operation.
- Historical Engram captures, ledgers, migrations, and decision records remain
  immutable provenance. Retirement does not imply deletion.

## Change classes and gates

| Work items | Change class | Gate |
|---|---|---|
| `BINK-3`, `BINK-4`, `BINK-6`, `BINK-7` | Structural | Preserve behavior; use only synthetic fixtures and disposable databases until a later ticket authorizes live reads. |
| `BINK-5` | Data migration | Export and import remain deterministic and manifest-verified; rehearsal precedes live data. |
| `BINK-8` | Semantic | Owned lexical search and the optional Engram liveness adapter require explicit parity and residual-risk evidence. |
| `BINK-9`, `BINK-10` | Deployment | Tailnet-fronted behavior, compatibility, recovery, monitoring, and restore checks must pass before authority changes. |
| `BINK-11` | Cutover and live data migration | Requires a separate owner-approved live window and one-writer rollback authority. |
| `BINK-12` | Consumer migration | Praxis probes must pass before compatibility shims can expire. |
| `BINK-13` | Retirement | Remove runtime wiring only after cutover and consumer migration; retain historical provenance. |

Passing a test verifies exercised behavior; it does not accept a deployment,
consumer migration, cutover, or retirement step.
