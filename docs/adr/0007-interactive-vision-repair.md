# ADR 0007: Restore interactive vision after model retirement

- Status: accepted under the owner's repair request
- Date: 2026-11-08
- Work items: BINK-49, BINK-50, BINK-51, BINK-52
- Supersedes: ADR 0005's served-model pin

## Evidence

The deployed `qwen/qwen3-vl-32b-instruct` returned HTTP 404 with an October 9,
2026 retirement notice. BinKeeper also sent Ollama-specific no-thinking flags
to OpenRouter. A synthetic probe of the suggested Qwen3.7 Plus replacement
took 46.1 seconds with those flags and 2.7 seconds with OpenRouter reasoning
disabled and latency-sorted routing. The request's 20-second socket timeout
did not enforce a total wall-clock deadline.

The [bounded comparison](../benchmarks/2026-11-08-interactive-repair.md)
tested three supported Qwen models on six existing photos, twice each.
Qwen3.8 Flash had the highest measured recall (0.71), valid JSON on all 12
calls, and mean latency of 4.6 seconds. These are sample measurements, not
accuracy or latency guarantees.

## Decision

Use `qwen/qwen3.8-flash` for interactive OpenRouter vision, retaining the
existing provider and model environment overrides. Send OpenRouter's
`reasoning.enabled=false` and `provider.sort=latency` controls through a
provider-specific client. The local client retains its Ollama request hints.

Analyze at most three cloud photos concurrently per photo-drop request, merge
in upload order, and retain the existing confidence and partial-failure rules.
Other callers remain serial unless they explicitly opt into concurrency.
Run advisory QR co-location harvesting after the response with its own local
database connection. Original photos are still stored before vision runs.

If vision fails, keep the saved-photo reference and show the reviewed manual
create/print form. Report storage failure separately. Print intent, replay
suppression, and explicit owner confirmation retain their existing contracts.

The owner's follow-up exposed a missing recovery path after leaving a failed
result page: originals remained in the vault, but the UI could not reopen
unattached uploads. `/saved-photos` now selects existing images through scoped,
serving-role reads and reuses the proposal and confirmation flow. Listing and
previewing photos do not invoke vision. Retrying requires an explicit POST
and does not store new blobs or harvest fresh location evidence from old photos.
Linked photos offer the existing bin's reprint controls. This adds no schema or
change to original evidence.

In the subsequent owner-directed workflow, Saved photos becomes an upload inbox:
save originals now, then explicitly create a bin or attach selected photos to
an existing bin. Attachments append scoped, idempotent capture links without
changing label, contents or location. Creation links every selected photo in
the registration transaction. Label and contents may be entered before analysis;
only blank fields are inferred, and supplying both avoids a model call.

## Scope and rollback

ADR 0004's downscaled-image-and-prompt export scope, BinKeeper's sole writer
authority, and all append-only evidence contracts remain in force. This change
does not alter the nightly ensemble, local OCR pin, printer defaults, or schema.

Rollback the wheel independently of the model environment pin. The retired
model cannot be restored as a working rollback. Select a verified supported
model or set `BINKEEPER_BIN_VISION_PROVIDER=local`, then restart the same
service. The existing quick-label page remains usable without vision.

The post-response co-location scan is best effort, not a durable job queue;
a forced service stop may interrupt it. A hard wall-clock vision deadline and
automatic model failover remain outside this repair.

## Shared admission repair (2026-11-09, BINK-58)

The direct upload and saved-photo routes now share the 12-photo application limit.
Every photo must be nonempty, decodable and at most 20 MiB before model work.
An invalid new batch is refused before storage; already saved selections remain
available. Accepted uploads are stored before inference admission.

The interactive service has one explicit worker and three shared model-call slots.
Cloud batches reserve up to three slots, local batches one, and label OCR one.
Admission does not wait: overload returns HTTP 429 with saved-photo references
and entered fields for manual review or retry. Reservations are released on success
or error; no background job is persisted. A failed photo analysis returns manual
review instead of presenting a partially completed batch as a finished proposal.
The per-request concurrency rule above no longer permits separate overlapping
requests to multiply the three-call limit.

Local journal events contain an opaque request ID, count, result category and
validation/storage/model/merge-and-orchestration/render durations. They contain
no photo, prompt, field contents or hashes. Model time is the union of active call
intervals, not their sum. These measurements are not a latency SLO or evidence of
production demand. No external telemetry is added.

Persistent drafts and jobs remain deferred. The BinKeeper maintainer owns a future
proposal if measured interruptions or repeated owner reports show lost edits need
durable recovery. Current accepted originals retain the existing vault lifetime;
unsaved fields live in the returned page only. Closing or abandoning that page
can lose those fields. No stale result auto-applies: creation is still a separate
reviewed action. A future draft/job design must set retention, expiry, input-version
binding, stale-result handling and backup semantics before implementation.
