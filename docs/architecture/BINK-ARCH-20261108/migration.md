# Migration and execution-ready work

No data authority moves. Each stage uses the existing BinKeeper package, database,
vault and service. This review has not implemented or deployed these stages.
The BinKeeper maintainer owns sequencing and acceptance; an implementing agent
must claim each item, record its branch/worktree and source revision, and complete
the project's verification/integration lifecycle. There is no external commitment
or new hosted persistence in this plan.

## Ordered work

| Work | Plane | Owner | Depends on | Completion evidence |
| --- | --- | --- | --- | --- |
| W1 cloud export refusal | BINK-54 | BinKeeper maintainer; executor unclaimed | — | Both client transports make zero calls on preparation failure; manual recovery preserved |
| W2 creation intent and recovery | BINK-55 | BinKeeper maintainer; executor unclaimed | — | Real-DB collision, midnight replay, concurrent allocation and complete error-form checks |
| W3 physical print accounting | BINK-56 | BinKeeper maintainer; executor unclaimed | BINK-55 | Restart/crash probes plus durable, honest intent/outcome display and no automatic retry |
| W4 permitted nightly route and freshness | BINK-57 | BinKeeper maintainer, using existing fleet/provider contracts | — | Permitted subscription/local route, explicit unavailable-state behavior and last-success/failure visibility |
| W5 admission and request-recovery measurement | BINK-58 | BinKeeper maintainer; executor unclaimed | BINK-54, BINK-55 | Consistent 12-photo policy, shared model budget proof, measured phases and interrupted-form scenario |
| W6 vault crash/recovery experiment | BINK-59 | BinKeeper maintainer; executor unclaimed | — | Isolated crash/missing/corrupt-object/restore evidence and a bounded follow-on decision |

W1 and W2 are the next repairs. W4 can proceed independently because its route
and missing local dependency already prevent reliable nightly review. W3 follows
stable creation identity; W5 builds on safe export/creation. W6 is an experiment,
not authorization to garbage-collect or alter owner originals. Exact IDs and
dependency identities are also in [work-items.json](work-items.json); Plane owns
work state. Dependency text is explicit because the installed public CLI does not
provide relation mutation. No native dependency edge is claimed.

## Stage 0 — preserve current authority and record the baseline

Keep append-only ledgers, originals, manifests and the source pin. Reproduce C1–C4
in disposable fixtures, and convert them into correctness regressions as each
repair lands. Separate code repairs from any new schema/format checkpoint.
Existing owner pages remain the operational path; direct review checks must use
synthetic data and installed-package hashes, followed by authorized tailnet
acceptance. Do not rerun models on owner photos simply to verify infrastructure.

No state copying or mixed writer is needed. The next implementing agent checks
that the baseline and runtime assumptions still apply; stale evidence is refreshed,
not treated as false. If a new worker, route or state authority is proposed, return
to the decision rather than silently adding it to these tickets.

## Stage 1 — enforce the advisory export boundary (W1)

Normalize at the final cloud-bound seam shared by callers, with a typed failure
before any transport call. Originals and supplied text remain local and reusable.
Old/new application versions share the same media schema; no original backfill or
rewrite occurs. Existing failed requests may be explicitly retried after the fix.

Acceptance includes malformed/unsupported input, transformation/encoder failure,
valid EXIF stripping, and both provider request shapes. A web-only validator is
insufficient because CLI/nightly clients use the same export contract. Retirement
means deleting the raw-byte fallback from every cloud-bound path.

Last safe exit is before cloud use of an unverified encoder. If the new path fails,
keep assistance unavailable or use the already authorized local route while
forward-fixing. Rolling back to unauthorized original-byte export is not recovery.
An absent owner can continue using saved photos/manual fields; no automatic route
fallback is introduced.

## Stage 2 — establish creation identity without changing physical registration (W2)

Use one concrete create operation with stable action UUID and canonical reviewed
payload. It owns allocation/validation of code, capture, initial place and all
selected links in one transaction. Divergent reuse is an explicit recoverable
conflict. Retain the separate operation for observing an already printed physical
label at a site; do not change its historical day-scoped records into new facts.

New forms carry the new identity. An older create form without it must not silently
enter the old mutation path after upgrade. Return its selection, text and print
choices for explicit reconfirmation under a new review, with no write on rejection.
Do not mint a fresh identity on every retried POST. Confirmation errors retain all
context; fresh GET draft recovery is a separate promise.

Acceptance runs C1/C2 with real DB state, concurrent identical/different actions,
code collision, date rollover and an intervening move. Existing attach replay,
quick-label and physical registration tests remain protected. New command metadata
or receipts must be readable by relevant projections, included in backups and
restorable. Prove those properties; do not infer compatibility from JSON storage.

Before the first new command write, code rollback is straightforward. Afterward,
any rollback binary must still recognize recorded action identities and reject
unsafe legacy creation. Otherwise freeze the affected create path and forward-fix,
leaving catalog, evidence and safe attachment available. Never delete receipts to
make an older version run. Retire the old photo-create adapter after stale-form
recovery and replay checks pass; leave historical evidence immutable.

## Stage 3 — make physical effects inspectable (W3)

Build on existing intent evidence, using an additive format if necessary. Reserve
the exact action, target, format, copies and payload identity before I/O. Append
only handoff outcomes that were observed. “Reserved without observed result” is
unresolved; “submitted” is not proof of paper. No worker may reclaim an unresolved
physical intent and automatically print it again.

Migrate photo creation, quick-label and manage reprint one entry point at a time,
while all paths share action identity and replay suppression. Existing v1/v2 intents
remain readable; do not manufacture missing historical outcomes. Fake-transport
process termination tests cover before/after submission and before response.
Optional physical acceptance is a separately authorized action.

The operator sees the receipt and inspects the printer before requesting a new
reprint. Operator absence leaves an explicit unresolved record; it does not trigger
retries. After new intent writes, rollback must preserve their suppression and
display. If the old binary cannot, disable only affected printing and forward-fix.
Retire response-only outcome handling after all three paths pass the same checks.

## Stage 4 — repair and observe the optional nightly lane (W4)

First reconcile the subscription policy: Opus must not be sent through OpenRouter.
Use a verified subscription adapter or the existing accepted local-only rollback;
do not infer authorization from a key or silently weaken the ensemble. Inspect
current exact-model lease availability before execution. No global fleet or model
configuration change is included by implication.

Preserve input-keyed proposal identity, strict affected-bin failure, owner theme,
contents-additions-only review and original observation times. Existing proposals
remain readable during route changes; model identity/version changes must be
explicit in new input keys. Verify outage, quota/auth refusal and retry behavior
with synthetic adapters before any authorized live run.

Report last success, failure reason and pending work separately from core readiness.
There is no justified freshness SLO yet; show observed timestamps rather than a
green claim of timely review. Operator absence leaves the optional lane visibly
degraded while catalog/manual workflows remain usable. A failed candidate route
returns to verified local mode or disabled/degraded operation, never forbidden API
fallback. Retire obsolete provider pins only after installed-route verification.

## Stage 5 — bound work before adding orchestration (W5)

Unify the 12-photo policy and explicit validation before inference. Add a shared,
documented model-call budget with an observable overload result; select its initial
value from the bounded experiment and existing operating constraints. Do not turn
admission into an unbounded hidden wait. Separate upload, queue/admission, inference,
merge and render timings, retaining accepted originals on failure.

Use two concurrent synthetic batches and a controlled slow model to verify the
aggregate bound and recovery path. Then run the leave/reopen experiment, recording
originals, selection order, owner text and destination separately. An RPO, latency
SLO, thread count or memory limit is not invented by this review.

If the product needs only intact returned error forms, stop after W2/admission.
If reliable draft resume is required, define a small local draft record/retention
policy and backup coverage before migration. If analysis must finish after browser
closure/service restart, specify job ownership, cancellation, stale results and
replay before adding persistent jobs. Neither condition currently earns a broker,
new service or second inventory authority. Any new durable writes create a separate
mixed-version/restore checkpoint; old code may ignore drafts, never lose facts.

## Stage 6 — verify media durability (W6)

Use a synthetic vault and disposable DB. Interrupt before/after file publication
and metadata commit; retry missing/corrupt ciphertext; inspect orphans; restore an
authenticated backup. Distinguish process death from actual host power loss.
Select fsync/publication/reconciliation changes only from the established contract
and evidence. Preserve every original and append-only row in production.

There is no production authority transfer or data copy in this experiment. Remove
only its exact disposable target. A later repair needs an explicit operator
diagnostic and safe action for each missing/orphan state, with no automatic deletion
or key rotation inferred from “cleanup.” Use the results to choose the next vault
repair; a generic storage coordinator is outside this experiment.

## Acceptance and closure

For each implementation: record source SHA, tests and limitations, deployed package
identity if deployment is authorized, fronted owner behavior, known in-flight work,
backup/restore proof for new persistent formats, and rollback limits after first
write. Numeric operating/time targets remain owner/measurement decisions where
unknown. Passing a test verifies that scenario; it does not authorize deployment
or accept a changed product guarantee. The review closes with this plan and owned
work, while the six implementation/measurement items remain open.
