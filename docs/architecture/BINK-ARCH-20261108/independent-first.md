# Independent first proposal — A1

Immutable selected record of /root/independent_architect's first proposal, returned
2026-11-08 before receiving any current-system source, ADR, topology or competing
proposal. Input: intent-only briefing in the coordination record. The lead has
not reconciled this candidate with existing behavior at this revision.

## Proposed shape

A local modular monolith, one transactional database, local original files,
bounded background work and an append-only inventory ledger. No separate service,
broker or distributed transaction is justified by the stated sparse owner workload.

The interaction is save evidence, then decide. A durable photo-batch manifest
identifies every successfully saved original. A batch becomes ready only when all
manifest entries are stored and verified; partial batches remain visibly resumable.
File/database crash recovery reconciles unfinished uploads without deleting evidence.

Resumable drafts contain selected photo IDs, destination, supplied label/contents,
field origins and revision. They survive browser closure and model failure.
Confirmation binds to that revision and atomically creates/updates the bin and
attaches the complete selected set, with a stable command identity for retries.

Accepted decisions append to the inventory ledger with provenance, confidence,
command identity and evidence references. Rebuilds consume decisions, never rerun
models. Operational upload/job/heartbeat state stays outside the fact ledger.

Persisted advisory jobs bind image selection, prompt/provider and draft revision.
Results are immutable suggestions; late results cannot overwrite owner text or a
newer draft. Only locally downscaled images and prompts use authorized routes;
subscription models fail closed when the permitted route is unavailable.

Rebuildable projections answer current inventory, containment/location, search and
pending review. Newly observed contents may be reviewed; non-observation never
removes contents or relabels a bin. Proposed location ordering uses committed
accepted moves, with claimed occurrence time retained separately; this assumption
needs reconciliation with existing location semantics.

Physical actions reserve owner-reviewed intent and a rendered artifact before
submission. Printer timeout means unknown, never automatic retry. The proposal
also treats trip reconciliation as proposed moves requiring owner confirmation;
that assumption needs comparison with existing trip-scan authority.

## Boundary records

| Boundary | Removal counterfactual and lost property | Displaced complexity |
| --- | --- | --- |
| Evidence store and batch | Every caller handles file integrity; no single saved-set answer | Upload/inference retry and repair code |
| Durable draft | Browser or inventory holds unfinished work; loses recovery or separates intent poorly | Browser storage or premature fact writes |
| Inventory ledger | Mutable views become authority; deterministic explanation/rebuild weakens | Secondary audit mechanisms |
| Advisory inference | Saving/editing/printing depend on slow model availability | Request timeout and field-race handling |
| Projections | Every screen interprets folds and evidence status | Repeated joins/folds in UI callers |
| Physical effects | Irreversible actions hide inside ordinary updates | Scattered retries and uncertain outcomes |

These are code/data boundaries, not deployment boundaries. The worker may run in
process with durable jobs, bounded concurrency/timeouts/retries and visible failures.

## Claims proposed for checking

1. With inference unavailable, owner can save, close, reopen, recover selections
   and supplied text, and finish without reuploading.
2. Three-photo confirmation attaches all three or none; crashes and retries cannot
   partially associate or duplicate the accepted command.
3. Late inference cannot overwrite supplied text, accepted labels or a newer
   selection, remove contents, or cause location/printing effects.
4. Rebuilding canonical projections reproduces inventory, containment/location,
   classifications and review identities without contacting model or printer.
5. After upload, save/confirmation acknowledgments do not wait for inference or
   printing. The architect proposed 500 ms as a provisional measurement target,
   not an owner requirement; host/storage measurement is missing.

## Migration and unknowns

Map current evidence/identities/write paths first. Introduce batch manifests and
drafts; prove completeness and recovery; introduce atomic confirmation; then move
inference behind persisted jobs and validate rebuild/effect recovery before retiring
old paths. Every stage preserves new evidence on rollback.

Unknown: existing original identity and history completeness, file/database durability,
printer observability, location ordering, migration cost. Preserve gaps as provenance
gaps. Assumptions: single owner, sparse bursts, modest local storage, no offline
multi-device writer requirement; concurrent tabs still need revision checks.
