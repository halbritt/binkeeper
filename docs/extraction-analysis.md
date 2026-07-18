# BinKeeper extraction analysis

Status: planning baseline

Date: 2026-07-18

Source baseline: Engram `a4b2b489c80370256d82256527428bd2b7714d6a`

Target repository: `https://github.com/halbritt/binkeeper`

Plane project: `BinKeeper` (`BINK`)

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
   Keep the schema capable of preserving the existing ids and timestamps.
4. Build a deterministic exporter from Engram and importer into a disposable
   BinKeeper database. Generate a manifest of row counts, stable ids, payload
   hashes, blob hashes, projected locations, trip checksums, and passports.
5. Extract label, vision, photo, catalog, management, CLI, and MCP surfaces.
   Keep network access local and preserve strict front/origin checks.
6. Add backup/restore and deployment checks. Rehearse a full cutover and rollback
   using synthetic fixtures, then a read-only copy of live data.
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

The Plane epic and children are the execution authority. The intended order is:

1. extraction ADR and preservation contract;
2. standalone package and green baseline;
3. owned persistence and blob authority;
4. deterministic exporter/importer and manifest;
5. domain, CLI, and MCP extraction;
6. photo, vision, label, catalog, and management extraction;
7. owned search plus optional Engram liveness adapter;
8. standalone service, tailnet route, and compatibility shims;
9. backup/restore and cutover rehearsal;
10. live cutover;
11. Praxis consumer update;
12. Engram retirement and decision supersession.

Items that alter live authority must remain blocked until their prerequisites
are accepted. Passing tests verifies implementation behavior; it does not by
itself accept a cutover.

## Risks and unresolved decisions

- The final compatibility-window duration is not yet accepted.
- The standalone tailnet URL and port are not yet selected.
- Blob key custody may require key reuse, rewrap, or re-encryption. The cutover
  work must choose one and prove restore behavior before moving live authority.
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
