# ADR 0006: Async label-drift proposals with an owner review queue

- Status: accepted
- Date: 2026-08-06
- Implemented: 2026-08-11 (`BINK-42`..`BINK-44`)

## Context

ADR 0005 fixed the interactive first pass at the benchmarked OpenRouter
Qwen3-VL 32B (0.70 mean item recall, ~4.3 s) because the owner's binding
constraint is photo-drop latency, and explicitly deferred a second, slower
enrichment pass to an owner-review queue. The benchmark showed what that
deferral leaves on the table: the union ensemble of `claude-opus-5` plus the
local `qwen3-vl:8b` reaches 0.83 recall (+0.13 over the deployed default, and
the only configuration that finds the AGR-003 powder/primers), at ~21 s and
~$0.02 per photo — fine where nobody waits, unacceptable interactively. Bin
passports also drift: contents change after registration, and nothing today
re-examines stored photos against the current passport.

## Decision

A nightly asynchronous second pass re-proposes bin labels from stored photos
(`propose_bin_label` behind the existing `VisionClient` seam), diffs each
proposal against the bin's passport at proposal time, and queues MATERIAL
diffs for owner review. Vision stays advisory everywhere: nothing
auto-applies; acceptance goes through the existing profile-correction path
(`bin_manage` profile snapshot), dismissal is itself append-only evidence.

Owner-accepted parameters (2026-08-06):

- **Model**: the `claude-opus-5` + local `qwen3-vl:8b` union ensemble (0.83
  recall), as a fan-out client behind the `VisionClient` seam (concurrent
  calls, items unioned in JSON space, cloud theme wins). Export delta stated
  explicitly: downscaled inference JPEGs and prompt text transit OpenRouter
  to **Anthropic as a new upstream model provider**, at night, under ADR
  0004's unchanged scope (vision lane only, downscaled JPEG + prompt only,
  output advisory). The local 8B leg runs on peecee after the 03:30 OCR
  true-up so nightly model swapping stays harmless.
- **Cost bound**: proposals are input-keyed — a bin is re-analyzed only when
  its photo set, passport, or the configured model changed (idempotency key
  over those inputs), so steady-state nightly cost is near zero and total
  cost scales with change rate, never vault size.
- **Evidence shape**: each proposal is one append-only capture of kind
  `label_drift_proposal` holding the proposal, the passport snapshot it was
  diffed against, the computed diff, the photo hashes and model versions
  used, and the idempotency key. The review queue is a rebuildable fold over
  proposals, dismissals, and profile snapshots — never a mutable table.
- **Materiality**: a proposal earns a queue entry when the normalized theme
  changes OR at least two detected items above the existing 0.35 confidence
  floor are absent from the passport. Non-material proposals are recorded but
  not queued. One queue entry per bin; the newest proposal supersedes.
- **Dismissal demotion**: a dismissed suggestion (bin + item label, or bin +
  theme) is demoted from materiality for 90 days (anchor-demotion precedent,
  commit `f75cb92`, deliberately fresher than its 180 d horizon). New photo
  evidence restarts consideration immediately.
- **Surface**: a pending-review section with a count badge on the catalog
  page (`/bins/`), each entry deep-linking to the bin's manage page where
  accept/dismiss actions sit next to the existing profile editor. Accept
  pre-fills the normal idempotent profile-snapshot correction; dismiss
  appends a `label_drift_dismissal` capture keyed to the proposal.
- **Retention**: proposals and dismissals are evidence — append-only, local,
  kept. Nothing new is retained in any cloud; the export is transient
  inference input under ADR 0004's scope.

Failure semantics: the nightly writer fails closed (no proposal is recorded
on a model error), runs autocommit like the OCR true-up so partial progress
survives, and reports a summary JSON to the journal; a failed night is
retried implicitly the next night because unchanged inputs are skipped only
once a proposal was actually recorded.

## Consequences

The owner gains a skimmable queue that catches what the fast first pass and
passport staleness miss, at a bounded, change-driven cost, without any new
interactive latency. Photo bytes reach a second upstream model provider
(Anthropic) on the nights a bin changed; the owner accepted this with the
benchmark and price in hand. The ~7 junk items per ensemble call land in the
queue where skimming is cheap, not in the drop page. Rollback is config:
pointing the drift lane's model at `local` (or disabling its timer) ends the
new export without touching evidence; recorded proposals remain valid
advisory history.

Rejected alternatives: hosted-32B solo (0.70 — adds almost nothing over the
first pass it duplicates), opus + hosted-32B (0.81 — loses the AGR-003-class
finds that only the local 8B makes), local-32B solo (0.61, free, no export —
kept as the documented no-cloud rollback for the drift lane). Revisit
triggers: junk rate high enough to cause queue fatigue, a benchmark rerun on
a grown photo set moving the standings, or OpenRouter/Anthropic pricing or
routing changes that break the ~$0.02/photo assumption.

Implementation slices (completed in Plane): (1) `label_drift_proposal`
evidence kind + the 04:00 timer ordered after the 03:30 OCR pass, (2) queue fold
+ read-only catalog surface, (3) accept/dismiss actions with idempotency keys.
