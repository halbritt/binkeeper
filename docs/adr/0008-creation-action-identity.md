# ADR 0008: Stable identity for new-bin creation

- Status: implemented and deployed for the owner-directed BINK-55 repair (2026-11-09)
- Date: 2026-11-09

## Context

Creating a new bin previously reused the physical-label registration identity:
code, site and day. Independent requests could silently share that identity and
cross-link photos; a retry after midnight could place the bin again and print
again. That identity remains appropriate for observing an already printed label,
whose historical evidence must remain unchanged.

## Decision

`create_bin` owns one reviewed creation action UUID and canonical payload:
requested code (possibly blank for allocation), site, label, contents, ordered
photo hashes and effective print choices. An append-only `bin_capture.v1` receipt
stores `creation_request` under a scoped `bin-create:<UUID>` idempotency key.
Existing passport/media projections read this capture and its separate photo
links without a new schema migration. Backup manifests protect the full capture
payload; authenticated restore tests prove replay recognition survives restore.

The action lock protects receipt lookup/replay. A code-prefix transaction lock,
shared with quick-label creation, protects allocation/validation until the receipt,
initial placement and every selected photo link commit. A supplied occupied code
is a recoverable conflict; an empty code allocates the next free site code.
An exact replay returns the original result without any new placement or links.
Divergent reuse conflicts, including changed photo order or print choices.

New forms explicitly distinguish creation from physical-label observation.
Missing/invalid creation identity and old forms return preserved review context
without writing. A new identity is issued for explicit reconfirmation, never as
an implicit retry of a failed POST. A consumed identity with changed details needs
the separate **Review as a new bin** action before another create confirmation.
Errors retain selected hashes, code, site, label, contents and print choices.
The page identifies a replay's site as the original creation site, without claiming
to change or describe the bin's current location.

No data authority moves; no historical evidence is rewritten. Stored originals
remain independent of form state. Fresh navigation still does not promise durable
draft recovery or background analysis. BINK-56 owns durable physical-print outcomes.

## Rollback and verification

Before new receipts are written, code rollback is possible. Afterward, retain the
new identity lookup and legacy-form refusal. An older binary that cannot recognize
these receipts must not serve creation; keep the upgraded path or stop affected
writes while forward-fixing. Never delete receipts or restore day-key creation.

Tests cover distinct-intent collisions, single/multiple photos, midnight replay
after a move, divergent payloads, concurrent identical/different actions, rollback
after photo linking, stale forms, returned-error context, explicit new review,
unchanged physical-label observation, fake-print replay and authenticated restore.
See [verification](../verification/architecture-repairs-2026-11-09.md).
