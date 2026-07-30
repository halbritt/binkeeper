# ADR 0003: Project physical bin containment from pack and unpack evidence

- Status: accepted
- Date: 2026-07-30
- Work item: BINK-38

## Context

BinKeeper already supports witnessed `LOC-` anchors. Those observations say
that a bin and a shelf or cabinet label appeared together in evidence; they do
not say that one physical bin is inside another. Reusing them for bin nesting
would turn advisory observations into authoritative mutable structure and make
location behavior ambiguous.

Physical nesting affects movement. If a small bin is packed into a larger bin,
the small bin follows the larger bin's effective site until it is unpacked.
This relationship must preserve BinKeeper's append-only evidence and
rebuildable-current-state invariants.

## Decision

BinKeeper records owner-attested `pack` and `unpack` events in a dedicated,
append-only evidence ledger. Folding that ledger produces a current
single-parent, acyclic containment graph; there is no editable parent column.
`LOC-` anchors remain separate, advisory witnessed evidence.

Packing requires both known bins to resolve to the same known site. It rejects
self-containment, cycles, and a second parent. A contained bin cannot receive a
direct place, load, or arrive event; the owner moves its outermost container or
unpacks it first.

A contained bin inherits the current location, confidence, and passport
location of its outermost container. Unpacking appends both the containment
event and an idempotent placement event for the child at the container's
effective site in one database transaction. That preserves the child's
location after it becomes top-level.

The CLI, MCP tool, catalog, and owner management page expose the same
projection. Web mutations remain writer-gated and strict-origin. Containment
event identifiers are scoped and derived from caller-supplied idempotency keys.

## Consequences

Current containment and effective locations are rebuildable from canonical
evidence. Moving an outer container changes every descendant's projected
location without rewriting descendant rows. Invalid graph transitions fail
closed and append nothing.

The implementation performs a scoped advisory lock around containment changes
and direct moves so concurrent requests cannot create a second parent, a
cycle, or move a child while it is being packed.

Rollback removes the new code paths and leaves migration 008 and any appended
events intact. Re-enabling the feature can rebuild the same graph. Evidence is
never deleted or rewritten during rollback.

An editable `parent_bin` column was rejected because it would erase history and
make current state non-rebuildable. Inferring physical nesting from `LOC-`
observations was rejected because witnessed co-location is weaker evidence and
does not identify another bin as a physical container.
