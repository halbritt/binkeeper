# BinKeeper extraction analysis

Status: cutover rehearsal in progress; authority unchanged

Date: 2026-07-18

Source baseline: Engram `a4b2b489c80370256d82256527428bd2b7714d6a`

Target repository: `https://github.com/halbritt/binkeeper`

Plane project: `BinKeeper` (`BINK`)

Accepted decision: [ADR 0001](adr/0001-standalone-authority.md), with owner
acceptance and operating defaults recorded in Plane `BINK-2`.

Implementation status: `BINK-3` through `BINK-10` establish the standalone
package, persistence, transfer, domain, owner workflows, search, and deployment
and durability boundaries. The frozen owner endpoint is loopback
`127.0.0.1:8766`, paired with tailnet HTTPS at
`https://proximal.tail0ecc2e.ts.net:8766`. Engram remains the runtime and data
authority until the owner-gated `BINK-11` cutover.
The service writer now fails closed by default, and the cutover transfer can
re-encrypt source blobs under a distinct BinKeeper key while preserving source
and target manifests. Neither change opens the production writer.

## Decision summary

BinKeeper has earned its own repository and release boundary. It has a distinct
domain, owner workflow, accepted invariants, and change cadence. Since 2026-06-20,
45 Engram commits touched the BinKeeper slice; only four changed BinKeeper paths
without also changing Engram integration or project records.

The first boundary should be source, package, persistence, and release
ownership. The evidence does not support starting with a network microservice.
BinKeeper can initially use the existing local PostgreSQL cluster, local model
runtime, local encrypted storage, CUPS, and tailnet host while owning its own
database, process, migrations, CLI, web app, and MCP interface.

The cutover must have one writer. Engram remains authoritative until a bounded
write freeze, export, import, verification, and service switch complete. There
must be no dual-write period. Engram's historical raw captures and append-only
ledger rows stay in place as frozen provenance after cutover; extraction does
not authorize deleting or rewriting them.

## Observed scope

The implementation at the baseline commit has:

| Surface | Observed size or state |
|---|---:|
| BinKeeper production Python | 22 files, 9,651 lines |
| Direct BinKeeper tests | 22 files, 7,200 lines |
| Direct test baseline with PostgreSQL | 295 passed in 171.71 seconds |
| BinKeeper-related commits since 2026-06-20 | 45 |
| BinKeeper-only commits in that interval | 4 |
| Live bin captures | 8 |
| Live move events | 5 |
| Live encrypted blobs linked to bins | 4 |
| Bin-linked blobs also referenced by non-bin captures | 0 |
| Live observation, presence, order, route, decision, and liveness rows | 0 |

The test command was:

```text
ENGRAM_TEST_DATABASE_URL=postgresql:///engram_test \
  .venv/bin/python -m pytest tests/test_bin*.py
```

The baseline proves the exercised Engram implementation, not the extracted
system. Each extraction slice must preserve or deliberately replace the
relevant tests.

## Current ownership map

### BinKeeper-owned behavior inside Engram

The main domain modules are `src/engram/bin_*.py`,
`src/engram/bin_catalog_web/`, and `src/engram/bin_photo_web/`. They cover:

- capture and registration;
- the append-only move ledger and trip checksum;
- confidence decay, observations, presence, resting orders, sweeps, and
  liveness;
- label rendering and bounded CUPS handoff;
- encrypted photo storage, local vision, catalog, and existing-bin management;
- bin passports, routing, placement receipts, and feedback folds.

### Engram dependencies that prevent an independent move

| Engram dependency | Why BinKeeper uses it | Required extraction treatment |
|---|---|---|
| `captures` and `sources` | bin capture evidence, profiles, photo links, print intents | Replace with a BinKeeper-owned append-only capture ledger. Preserve source identifiers as inert provenance instead of retaining an Engram foreign key. |
| `evidence_blobs` and blob-vault code | encrypted photo metadata and ciphertext access | Give BinKeeper its own blob metadata, configuration, key reference, and backup contract. Copy only the four bin-linked blobs after a decrypt/restore rehearsal. |
| `segments` and ordinary Engram search | transcript-deixis liveness and contents discovery | Implement owned exact/lexical inventory search. Treat transcript liveness as an optional Engram adapter that emits bounded observations; do not read Engram tables from BinKeeper. |
| `engram.abstain` | confidence floor and abstention | Move the small gate behind a BinKeeper-owned interface and fixtures. |
| `engram.personal_memory` | append-only capture creation | Replace it with a BinKeeper capture writer and idempotency contract. |
| `engram.db` and database roles | owner and serving connections | Create BinKeeper database roles, connection helpers, migrations, and least-privilege grants. |
| `engram.web.*` | path handling, chrome, origin/front checks | Port the verified behavior into BinKeeper-owned web modules. Do not keep a runtime dependency on the Engram package. |
| Engram CLI and MCP | operator commands and two public tools | Add a `binkeeper` CLI and `binkeeper.*` MCP names. Keep time-bounded Engram compatibility shims until consumers move. |
| Engram operator web and systemd unit | `/bins/` and `/bin-photo/` hosting | Add a separate BinKeeper process and tailnet-fronted route only after recovery and compatibility gates pass. |
| Engram backup/restore | database and blob durability | Add BinKeeper backup, restore-smoke, age checks, and runbooks before authority changes. |

### BinKeeper-owned tables currently in the Engram database

- `bin_trip_events`
- `location_observation`
- `bin_presence_events`
- `bin_resting_order_events`
- `bin_routing_requests`
- `bin_placement_decisions`
- `bin_item_liveness`

`bin_placement_decisions` has a foreign key to `bin_routing_requests`.
`captures` has a foreign key to Engram `sources`. The other listed BinKeeper
ledgers have no cross-domain foreign keys, but all use Engram's append-only
trigger functions, roles, migration runner, and backup boundary.

## Recommended target shape

### One deep domain interface

Callers should use a small BinKeeper interface that owns the domain behavior:

- locate or attest a bin;
- record a move or trip event;
- search inventory evidence;
- project a bin passport;
- recommend and record placement;
- manage photos, profiles, and label intents.

The interface includes authorization, idempotency, confidence, provenance,
temporal behavior, and failure semantics. Database rows and projector internals
are not part of it. Praxis and future consumers should depend on this interface,
not import tables or Python implementation modules.

### Local topology

The least costly independent topology is:

```text
owner browser / CLI / MCP
          |
          v
  BinKeeper process
    |       |       |
    v       v       v
local PG  local   local CUPS
database  blobs   and vision
```

Use a dedicated `binkeeper` database and roles on the existing local PostgreSQL
cluster first. A different host, container stack, or network service is a later
decision and needs a concrete isolation or deployment driver.

### Data authority

After cutover, BinKeeper owns all new physical-inventory evidence and derived
state. Engram may consume a narrow, read-only summary or evidence reference, but
must not write BinKeeper tables or mirror them as current truth.

Praxis already models BinKeeper as a physical-truth witness. Its current design
uses `engram.bin_where` and `engram.trip_scan` names and lives under a deferred
module. Update that adapter to the standalone interface before the Engram shims
are removed.

## Preservation contract

| Invariant or behavior | Cutover proof |
|---|---|
| Location is a fold over append-only move events | Import all events with stable ordering; compare projected location for every bin. |
| Trip reconciliation cannot lose a loaded bin | Compare trip checksums and `unaccounted` sets for every imported trip. |
| Retries are idempotent | Preserve `(tenant_id, corpus_id, external_id)` identity and rerun duplicate-write tests. |
| Raw captures and owner decisions are immutable | Import ids, payloads, observed/recorded times, privacy class, and hashes; install append-only triggers before writes open. |
| Current profiles are projections over snapshots | Compare passports field by field, including explicit clears. |
| Photos remain private and decryptable | Verify plaintext hashes after restore; confirm no raw hash, key, EXIF, or object path reaches HTML. |
| Printing requires reviewed owner intent | Preserve separate registration and reprint intents, replay suppression, timeout-as-unknown, and strict Origin checks. |
| Vision is advisory | Characterization tests must show that proposals cannot register, move, print, or accept placement without owner action. |
| Serving is least privilege | A serving role can read catalog data and media metadata but cannot write evidence. |
| Tailnet is the owner access path | Run the fronted HTTPS browser smoke against the standalone process. |
| Backups cover the new authority | Restore database and bin-linked blob bytes into a disposable target and compare counts, hashes, folds, and routes. |

## Migration strategy

1. Record the accepted extraction decision, ownership, public interface,
   compatibility window, and rollback rules.
2. Scaffold the package and move pure modules with their tests. This is a
   structural change only; no live writer changes.
3. Create BinKeeper-owned migrations, roles, capture ledger, and blob metadata.
   **Implemented in BINK-4:** the initial forward migration owns all nine
   evidence ledgers, serving is read-only, and synthetic blob restore verifies
   ciphertext and plaintext hashes. Production database and key provisioning
   remain part of the gated deployment/cutover sequence.
4. Build a deterministic exporter from Engram and importer into a disposable
   BinKeeper database. Generate a manifest of row counts, stable ids, payload
   hashes, blob hashes, projected locations, trip checksums, and passports.
   **Implemented in BINK-5:** export is read-only and pinned to the accepted
   source schema; import has only a target connection and verifies every
   protected manifest dimension before committing.
5. Extract label, vision, photo, catalog, management, CLI, and MCP surfaces.
   Keep network access local and preserve strict front/origin checks.
   **BINK-6 complete:** domain, persistence adapters, CLI, and MCP schemas are
   standalone and direct parity tests pass. **BINK-7 complete:** encrypted
   media projection, advisory local vision, catalog, reviewed management,
   registration, and bounded label handoff now run behind BinKeeper-owned
   modules with synthetic/disposable tests. No live route or hardware was used.
   **BINK-8 complete:** exact and lexical inventory search reads BinKeeper-owned
   evidence, and optional transcript liveness accepts only a versioned inert
   local export. Adapter absence is explicit and no model, embedding, cloud,
   or Engram table read was added.
6. Add backup/restore and deployment checks. **BINK-9 complete:** the standalone
   service composes health, readiness, catalog, media, and reviewed authoring on
   the frozen loopback port; the existing tailnet front passed an HTTPS smoke
   rehearsal; and stopping the process produced an explicit 502 rather than a
   stale Engram fallback. Production enablement remains part of `BINK-11`.
   **BINK-10 complete:** authenticated chunked database dumps, exact encrypted
   blob copies, signed recovery manifests, backup-age readiness, an unmocked
   disposable restore drill, local scheduling templates, and exact
   backup/cutover/rollback/retirement runbooks are implemented. Rehearse a full
   cutover and rollback using synthetic fixtures, then a read-only copy of live
   data. **BINK-11 rehearsal preparation:** blob staging verifies and decrypts
   the local Engram source, re-encrypts under a distinct BinKeeper key, and
   preserves separate source and target manifests. The standalone HTTP writer
   is frozen by default and refuses every non-safe method before dispatch.
   Current live-snapshot rehearsal does not authorize the production freeze,
   writer switch, or compatibility activation.
7. Freeze Engram BinKeeper writes, run the final export/import, compare the
   manifest, switch the owner surface, and observe a bounded verification
   window. Roll back by restoring the Engram writer if any protected projection
   or physical-action path differs.
8. Update Praxis and any other consumers. Remove Engram compatibility shims only
   after consumer probes pass.
9. Supersede Engram decisions and docs that say BinKeeper lives inside Engram.
   Keep historical migrations and frozen data for provenance unless a later
   explicit retention decision authorizes something else.

## Work breakdown

`BINK-1` is the parent campaign work item. Its children are:

| Item | Slice | Change class | Blocked by |
|---|---|---|---|
| `BINK-2` | Accept the extraction ADR and freeze the preservation contract | Decision | — |
| `BINK-3` | Scaffold the standalone package and preservation test harness | Structural | `BINK-2` |
| `BINK-4` | Create BinKeeper-owned database, roles, migrations, and blob authority | Structural | `BINK-2` |
| `BINK-5` | Build deterministic Engram export and BinKeeper import manifests | Data migration | `BINK-4` |
| `BINK-6` | Extract domain modules, CLI, and MCP into BinKeeper | Structural | `BINK-3`, `BINK-4` |
| `BINK-7` | Extract photo, vision, label, catalog, and management surfaces | Structural | `BINK-4`, `BINK-6` |
| `BINK-8` | Own inventory search and isolate transcript liveness behind an Engram adapter | Semantic | `BINK-4`, `BINK-6` |
| `BINK-9` | Deploy standalone BinKeeper with tailnet HTTPS and compatibility shims | Deployment | `BINK-7`, `BINK-8` |
| `BINK-10` | Add backup, restore-smoke, monitoring, and cutover runbooks | Deployment | `BINK-4`, `BINK-7`, `BINK-9` |
| `BINK-11` | Rehearse and execute the one-writer production cutover | Cutover and live data migration | `BINK-5`, `BINK-9`, `BINK-10` |
| `BINK-12` | Move Praxis to the standalone BinKeeper witness contract | Consumer migration | `BINK-9` |
| `BINK-13` | Retire Engram BinKeeper runtime wiring and supersede Engram decisions | Retirement | `BINK-11`, `BINK-12` |

Items that alter live authority must remain blocked until their prerequisites
are accepted. Passing tests verifies implementation behavior; it does not by
itself accept a cutover.

## Accepted operating defaults and unresolved decisions

- Engram compatibility shims remain until `BINK-12` probes pass, with a hard
  maximum of 30 days after production cutover. Extension requires a new owner
  decision.
- The standalone endpoint is frozen to loopback `127.0.0.1:8766` and tailnet
  HTTPS `https://proximal.tail0ecc2e.ts.net:8766`; Engram retains `:8765`
  throughout the compatibility window.
- The four current bin-linked blobs will be re-encrypted under a
  BinKeeper-owned key. Migration and backup/restore must verify plaintext
  hashes before authority moves.
- The owner or executing operator may restore the Engram writer during the
  verification window after a manifest mismatch, incorrect protected
  projection, failed physical-action path, or failed backup/restore check.
- Engram search currently provides contents discovery. The standalone minimum
  is exact and lexical search; semantic search should be added only if owner
  queries demonstrate a need.
- Transcript liveness reads Engram captures and segments. It should remain
  optional until an adapter contract avoids shared-table reads.
- The data set is currently small, but data-integrity risk is high. Small row
  counts make a manifest comparison cheap; they do not justify skipping it.

## Doctrine receipt

The architecture review used Pincite packet `pkt-90c0399541e8e0cc`, content
hash `90c0399541e8e0cc62f60b2b6e1aa38d57d1cdc12887df71e82eade0564d6441`,
corpus `corpus-2026-07-12-d2ea7b94a1ce`, doctrine
`doctrine-a90ee3f1cf7b6f26`, and retriever
`retriever-1392f38f05a41086`.

The packet's authority ceiling is recommendation. The owner request authorizes
repository and tracker setup, not the live migration. Missing evidence about
the final transaction plan, partial-failure behavior, compatibility duration,
blob-key transition, and service topology is material to cutover and is routed
to Plane work. Missing per-edit classification is nonmaterial to this planning
artifact because no implementation was moved in this slice.
