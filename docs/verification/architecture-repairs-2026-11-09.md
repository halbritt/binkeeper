# Architecture repair verification — 2026-11-09

The repairs follow the [system review](../architecture/BINK-ARCH-20261108/README.md).
BinKeeper remains the sole data authority. Tests use synthetic state and fake
model/printer transports. Deployment is recorded separately from source checks.

## BINK-54: refuse unsafe image export

Base: `81f93b7`, repository `halbritt/binkeeper`, branch `master`.
Both cloud clients now raise `ImagePreparationError` before transport when image
preparation fails. The local client uses the same preparation contract. Original
bytes are never a fallback. Successful JPEG preparation clears carried metadata;
the new test also reproduced JPEG-comment leakage on the previous successful path.

Verification: `BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_agent1331_repairs
make check` passed: **632 tests**, **20 migration tests**, lint, type checking and
wheel verification. The original regression failed for both cloud clients before
the repair. Tests verify malformed/unsupported/truncated images and injected import,
decode, orientation, conversion, resize and encoder failures make zero HTTP calls.
Valid synthetic images retain orientation, fit the 1280-pixel bound and contain no
original EXIF or JPEG comment. HTTP upload and saved-photo tests preserve originals,
photo references, site, code and supplied fields, with manual review still available.

Existing transport tests now use generated JPEGs so they still reach their intended
transport/response assertions. No live provider request, owner-data write, physical
print, schema change or production deployment was used for these checks. Fronted
installed-package verification is recorded below. Rollback must not restore
the raw-byte fallback: disable cloud assistance or select the accepted local route
while forward-fixing.

The repair applied `implementation-explicit-failure-policy` and
`universal-repository-contract-precedence` from Pincite packet
`pkt-61c47d23247950dd` (release `6deb1af41d41075b28577bcf2126c9db90d0e7c0`,
corpus `corpus-2026-07-12-84d6cdfc0e9f`, doctrine `doctrine-2694f0450e78ff1e`).
Packet SHA-256: `61c47d23247950dd5dc04fbbe0aa48a00a121a9eaa14505dbdc2f73798035103`;
retriever: `retriever-8b654e47fcb1be28`.
The retrieval gate passed. Material export/recovery questions are answered by the
accepted ADR, both transport probes and workflow regressions. Remaining generic
obligations about incident response, mutable graphs, Python special methods,
non-ASCII text, unchanged resource ownership and structural-change procedures are
nonmaterial to this bytes-to-image refusal repair. Production incident frequency
is unmeasured; installed behavior is checked separately below.

## BINK-55: stable creation and complete returned review

Base: `90b6041`, same repository and `master` checkout. The concrete creation
operation and compatibility/rollback contract are in
[ADR 0008](../adr/0008-creation-action-identity.md). This is additive capture
metadata, with no rewritten historical records or new SQL migration.

The new collision test failed against the old route, which reported success for
a second distinct intent using an occupied code. The repaired path rejects that
case and retains its fields. Tests cover zero/one/multiple photos, ordered photo
identity, all selected links, day rollover after an intervening move, divergent
payloads, four concurrent identical/different actions, rollback after linking,
unavailable DB recovery, old forms, explicit new review and print replay suppression.
The physical-label registration path retains its day-scoped semantics and cannot
invoke the photo-creation printing path.

`make check` with the disposable PostgreSQL database passed **652 tests** and
**20 migration cases**, lint, types and wheel verification. Two additional collision
variants (single and multiple photos) were then added and passed the targeted
creation suite. The existing authenticated backup/restore drill now includes a
creation receipt and photo link; restored identity replay adds no capture or move,
and unchanged passport projections read the receipt correctly.

UIPass run `1c06be56-ff90-43cd-915d-7b3f9421e51d` passed **24 cases**, no warnings
or blocking findings, for the proposal/recovery/replay and registration surfaces
at 320, 390 and 1280 pixels. Conflict and replay screenshots were inspected at phone
and desktop widths; the narrow vision-error screen was also inspected. The review
corrected an inconsistent synthetic photo count, removed an unjustified storage
claim from error reviews, and made explicit new review the only action for a
consumed identity. A replay now labels its original site as historical and links
to the current bin page, without claiming another placement.

A candidate process with its own disposable database passed a real Tailscale HTTPS
rehearsal on port 18777: create/replay returned 200, divergent payload 409 and
missing Origin 403. Replays left exactly one capture and one move. No model or
printer I/O was allowed. The temporary front, process and database were removed.
This verifies the candidate through the front; it is not a production deployment.

## BINK-56: durable printer handoff

Base: `a8e5680`, same repository and `master` checkout. Photo creation, quick
labels and management reprints now use one committed reservation before printer
I/O. New v3 intents include copies and the exact payload hash, format and target.
An append-only outcome records submitted, failed or unknown only when observed.
A reservation without an outcome remains unresolved; replay never reclaims it.
Management shows recent requests and their observed status. Submitted means the
transport accepted the request, not confirmed paper output.

`make check` passed **666 tests**, **20 migration cases**, lint, types and wheel
verification. Tests terminate separate processes before transport, after transport
and before the response, then replay in a new process: no second printer attempt.
Concurrent same-action requests also make one attempt. Failure, timeout, B1
uncertainty, reservation failure and lost outcome writes have explicit coverage.
Legacy v1/v2 reservations suppress replay even when the requested backend changes;
missing historical copies/outcomes are not invented. The authenticated backup/
restore drill preserves both the reservation and outcome and suppresses replay.

UIPass run `ad3c7aec-4b65-40ae-84a9-2fb98353f965` passed 12 management and quick-label
cases at three widths with no findings. Phone and desktop print-history screenshots
were inspected: the unknown status, timestamp, printer, copies and check-before-
reprint instruction remain readable, with the deliberate reprint action below.
The combined candidate front rehearsal recorded under BINK-58 also covers this
route. No physical printer call or production service change occurred in this check.

Rollback retains all print evidence. If an older binary cannot preserve the
reservation contract, disable its affected print actions and forward-fix; never
retry an unknown effect automatically. No migration or evidence rewrite is needed.

## BINK-57: authorized local review and visible freshness

Base: `4cb228e`, same repository and `master`. Source default and service pin now
select ADR 0006's explicit local-only rollback. Explicit ensemble configuration is
refused before model I/O regardless of API-key presence. No subscription adapter
is claimed. Fleet selection still requires exact `qwen3-vl:8b`, 32768 context and
batch latency, with no endpoint/model fallback or automatic replay.

Read-only fleet inspection at 2026-11-09 18:21 UTC, gpu-fleet revision `2de2007`,
found two declared 8B slots but neither routable: both were below their loadability
thresholds. Heartbeat was active; the installed nightly service had exited 75 at
04:08 local time. Its installed pin still selected the old ensemble. This repair
did not change fleet capacity, installed configuration, or run owner photos at
that source checkpoint. The installed configuration was updated during deployment
below; fleet capacity remains a separate dependency.

`make check` passed **674 tests**, **20 migration cases**, lint, types and wheel
verification. Routing refusal, failure/resume, unreadable photos, stale/interrupted
status and corrupt/missing status are covered. Failed input resumes once; already
recorded input is skipped. Proposal-only runs preserve theme, contents, location
and print history until owner review. A real transient user-systemd rehearsal
ran a synthetic exit-75 command, then `/bin/true`: start/stop hooks recorded the
failure and later success while retaining both timestamps. No model ran.

Status is a replaceable local file owned by the service's StateDirectory. It is
not inventory evidence or a core readiness dependency. Catalog reads show last
completion/failure and a safe reason, including a lease failure before the CLI
starts. Deployment must install the wheel before units because the new hooks
need the new entry point; checking the new unit against the old installed wheel
correctly reports that executable missing.

UIPass final run `72f598b8-8567-481d-8870-b88145767077` passed 15 catalog cases at
three widths with no findings. Phone/desktop degraded-status screenshots and the
narrow expanded-review screenshot were inspected. The first run exposed a real
320-pixel clipping defect in the existing contents panel; wrapping its summary
and allowing its grid width to shrink fixed that defect and all 18 associated
contrast-assessment warnings. Times now use readable UTC text.

## BINK-58: bounded photo admission and measured recovery

Base: `0a33909`, same repository and `master`. Invalid new batches are refused
before storage/model work; accepted originals are saved before admission. Direct
uploads, saved uploads and saved selections use the same 12-photo, 20-MiB-per-photo
and decode checks. One service worker shares three inference slots, with immediate
recoverable 429 on overload. Label OCR uses the same budget. Model errors preserve
manual review and never automatically register or print.

`make check` passed **684 tests**, **20 migration cases**, lint, types and wheel
verification. Two overlapping 13-photo batches make zero model/storage calls. A
12-photo blocked cloud batch and overlapping direct/saved attempts never exceed
three active calls; both refused attempts preserve accepted originals and fields.
After release, retry succeeds; evidence still contains no inventory/print mutation.
Malformed, empty and oversized input tests cover both upload paths. Export-failure
recovery tests now inject failure at the export-preparation seam, after successful
input validation, preserving their original intended boundary check.

The bounded 64x64 synthetic overlap run took 93.45 ms, including deliberate model
blocking until recovery assertions finished. Its 12-photo request measured 1.571 ms
validation, 6.307 ms storage, 77.351 ms model-active time, 0.483 ms merge/orchestration
and 0.284 ms render. A separate 1600x1200 browser fixture through actual tailnet
HTTPS measured 48.305/10.374/277.467/0.102/15.875 ms for those phases. These are fake
model timings and do not predict live provider latency, abandonment rate or an SLO.

The real Chromium front rehearsal navigated away while inference was blocked,
closed/reopened the page, found the saved original and retried successfully.
Unsaved fields were empty on reopen: durable draft recovery is explicitly deferred
in ADR 0007. HTTPS create/replay returned 200, divergent creation 409 and missing
Origin 403. Fake printing produced exactly one creation attempt and one deliberate
reprint despite replay. The management page showed the two-copy handoff; the catalog
remained usable with both current and degraded advisory status. The isolated
process, database and temporary Tailscale port 18777 were removed. No live owner
image, provider call, physical printer or production deployment was involved.

UIPass run `d8e37e0c-1a72-4921-9090-c519f2d51201` passed 48 photo, saved-photo and
result cases at three widths without findings. Busy-result phone/desktop screenshots
were inspected; preserved fields, stored-photo status and manual/retry actions are
visible. A final copy pass removes an overly broad safety claim and clarifies that
confirmation, rather than rendering the form, saves bin details.

Final copy-only UIPass run `75a426fb-7295-40e3-9c39-f53aac21bc99` passed 24 result
cases without findings; busy phone/desktop screenshots were inspected again.
The affected web/service tests passed 81 cases after the copy change.

## BINK-59: vault publication and recovery

Base: `7c192ec`, same repository and `master`. Five synthetic termination windows
were run before implementation. Unpublished work had no metadata; publication
before commit left an orphan; committed ciphertext authenticated after restart.
Explicit retry recovered the orphan without rewriting accepted evidence. Additional
regressions reproduced duplicate-save success with missing/corrupt ciphertext,
unexercised flush failures, and a four-writer autocommit race. No owner-data loss
is inferred from these tests.

A separate implementation checkpoint was recorded in BINK-59 after those results
and before changing the writer. The repair keeps keys, encryption and metadata
formats unchanged. It owns the transaction around the digest lock, validates
existing objects before duplicate success, and flushes the temporary file and
publication directories before metadata insertion. Missing/corrupt evidence is
refused rather than silently rewritten; orphan cleanup is not added.

The repaired targeted run passed **14 cases** (10 vault and 4 backup), including
file/directory flush failures and four concurrent writers. The existing encrypted,
HMAC-authenticated PostgreSQL/blob restore drill passed with creation and print
receipts. A pre-repair module extracted from the previously verified wheel read
new output, and the new reader authenticated old output and replayed its metadata.
Both directions left exactly two synthetic blob rows. The compatibility database
and temporary vault were removed. Probe hashes/states are preserved in
[vault-recovery-2026-11-09.json](vault-recovery-2026-11-09.json).

The runbook enumerates known-good, unpublished, orphan and missing/corrupt states.
Physical power-loss behavior remains unmeasured; filesystem flush success and a
process-kill experiment do not verify the storage controller. Production keys,
originals and existing backups were not changed by these experiments.

The complete BINK-59 `make check` passed **694 tests**, **20 migration cases**,
lint, type checks and wheel verification. The new writer does not change the
backup or ciphertext format. Deployment and installed front checks follow below.

## Deployment and acceptance

All six repairs were installed on 2026-11-09 at 18:47 UTC. Runtime source is
`a4c4dfb` on `master`. The final wheel's SHA-256 is
`3c89e79facf806b6c20c54689ea8857424984ca034b2e7f66fdab46fb009fdb7`;
105 installed package files matched it byte for byte. After the complete gate
above, a final malformed-status regression passed all 8 advisory-status tests:
an invalid JSON state type now reports unknown instead of blocking the catalog.
The wheel was rebuilt and verified after that change.

The production Python 3.12 environment passed synthetic image preparation,
filesystem publication and app construction checks. A disposable PostgreSQL
rehearsal with the installed package verified creation replay and print replay,
with exactly one fake printer submission. Development checks used Python 3.14.

Deployment replaced the wheel, installed the local-only nightly pin and status
hooks, reloaded systemd, and restarted the existing inventory service. The
nightly timer remains active. Unit validation found no BinKeeper error. No
manual nightly run, owner-photo model request or physical print was performed.
Canonical owner evidence, keys and existing backups were unchanged.

Through the existing Tailscale HTTPS front, `/healthz`, `/readyz`, `/`,
`/saved-photos` and `/bins/` all returned 200; readiness was true. The upload page
showed the admission limit, and the catalog exposed the advisory failure without
blocking inventory. These checks completed at 18:47:28 UTC. The installed unit
and pin matched their repository versions.

The new operational status file was initialized from the actual prior systemd
failure at 04:08:33 local time on November 9 (exit 75, lease unavailable/lost).
It records no invented success or new model run. A second read-only fleet check
at 18:48 UTC still found no routable exact `qwen3-vl:8b` capacity. **BINK-60**
tracks restoring that capacity and verifying a lease-backed synthetic run;
neither fleet configuration nor unrelated GPU work was changed here. This is
the remaining operational dependency for nightly contents review.

Rollback preserves creation, print and blob evidence and the explicit local-only
route. Keep affected actions disabled if an older package cannot honor their
receipt contracts; forward-fix rather than replaying an unknown physical effect
or restoring original-byte export. No schema rollback or evidence rewrite is
required. Probe databases, the temporary HTTPS front and synthetic vaults were
removed; committed verification contains only synthetic results.
