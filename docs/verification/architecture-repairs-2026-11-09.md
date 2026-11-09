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
installed-package verification remains a deployment check. Rollback must not restore
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
and installed behavior are unmeasured; no claim about either follows from the suite.

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
The final combined candidate front rehearsal will cover this route before release.
No physical printer call or production service change occurred.

Rollback retains all print evidence. If an older binary cannot preserve the
reservation contract, disable its affected print actions and forward-fix; never
retry an unknown effect automatically. No migration or evidence rewrite is needed.
