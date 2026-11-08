# Claims, checks and evidence limits

Subject: implementation at `ed8ae090aa021c9b61a0752eabbe8dc321ab8595`.
All claim results below apply to that revision and stated conditions. A passing
probe assertion reproduces the finding; it does not mean the defect is repaired.
No owner-data mutation, real model request or physical print was performed.

| Claim | Check kind / method executed | Result and applicability |
| --- | --- | --- |
| C1: different photo-creation intents can share one registration identity | Behavioral adequacy, A5 real disposable PostgreSQL and actual confirmed domain path, fake printer | **Supported.** Same code/site/day: second single-photo confirmation reports done but leaves its photo/text unlinked; multi-photo confirmation appends its two photos to the first bin. Different-code control succeeds independently. Collision frequency was not measured. |
| C2: replay after midnight can move and print again | Behavioral adequacy, A5 controlled registration clock, real capture/move folds, fake printer | **Supported.** Same-day replay preserves intervening move; after midnight the same confirmation appends another capture/place, restores old site and causes a second print call. Positive print request required. Does not apply to quick-label/manage UUID paths. |
| C3: image-preparation failure can export original bytes | Behavioral adequacy at final Python request boundary, A6 spies for OpenRouter and Gemini; socket calls forbidden | **Supported.** Malformed 38-byte input sent unchanged. Injected processing failure sends original 75,936-byte 3000×1600 JPEG with synthetic EXIF. Normal preparation yields 14,388-byte 683×1280 JPEG without tested EXIF. No actual provider receipt or production frequency claim. |
| C4: per-request fanout is not a shared application budget | Behavioral adequacy, A6 two concurrent ASGI requests, blocking fake clients/storage | **Supported, narrowed.** Each 13-photo request starts three calls; six aggregate, 26 total, HTTP 200 for both providers. Framework limits exist; no claim of infinite concurrency, actual throughput, memory exhaustion or production overload. |
| C5: printer recovery records are incomplete/inconsistent | Correspondence, source trace of manage reprint, quick-label and photo creation | **Supported within source scope.** Manage reserves before I/O; quick-label embeds partial intent; photo creation has no separate reservation. Outcomes are response-only. Process death and physical printer behavior were not fault-injected; W3 must do so. |
| C6: the nightly review is currently degraded and route policy needs repair | Deployment correspondence, systemd result/journal plus selected installed pin and subscription-route observations | **Supported for latest run.** Exit 75 at 04:07 PST: no routable local 8B slot. Installed cloud pin names Opus via OpenRouter despite current subscription-only rule. No model was executed; earlier run history and future availability are not inferred. |
| C7: saved photos do not imply durable unfinished intent | Correspondence, fresh GET defaults, template/script state, confirmation error branch | **Supported within source scope.** Original reuse exists; draft selection/text/destination are form-only. `_register_and_view` error retains code/contents but omits other reviewed context. Browser interruption experiment is deferred; returned-error repair is included in W2. |
| C8: file/DB power-loss durability is established | Correspondence/source inspection only; no power-loss experiment | **Inconclusive.** File write/rename has no explicit file/directory fsync; blob metadata follows file publication; deduped metadata returns without checking ciphertext. These facts do not prove observed loss. W6 specifies the discriminating experiment. |

## Executed A5 experiment

The verifier created `binkeeper_arch_agent1315_a5` from `template0`, applied all
eight migrations, stored seven synthetic encrypted JPEG originals, and called the
real `_register_confirmed` path. Only registration time and the physical printer
handoff/queue were substituted. Capture, move, link and projection functions were
real. Both findings and same-day/distinct-code controls passed their assertions.

For the colliding bin, the multi-photo case ended with three captures, one place
and one fake print. The midnight case ended with two captures, three place events
(including the intentional intervening move) and two fake prints. The disposable
database was dropped; a follow-up catalog query confirmed it absent.

The exact executed command was `bash .../a5/run.sh` in the private artifact root
listed in [probe-manifest.json](probe-manifest.json). The script asserts the source
SHA, creates only the named disposable DB and traps its removal. `a5/probe.py`
and `a5/results.json` preserve the experiment and canonical synthetic snapshots.
This command is an observation from the inspected host, not a portable install
instruction or a command against production.

## Executed A6 experiment

The verifier ran `PYTHONPATH=src .venv/bin/python .../a6/verify.py` in the source
checkout. It pins HEAD, replaces HTTP with request spies, rejects socket connection
calls, and uses in-process ASGI clients. All input images and metadata are generated.
It tests malformed bytes, successful preparation, import failure and an injected
`exif_transpose` processing failure for each cloud client.

The import-failure case proves the client seam; it does not claim that the photo
web module can start without Pillow. The processing-failure case needs no missing
dependency. Installed test-framework defaults cap multipart files and threadpool
work, so “unbounded service” would be an unsupported conclusion. The test observes
six admitted calls and a 13-photo batch, not the maximum possible concurrency.

## Runtime correspondence and existing verification

Read-only HTTPS probes and package hashes are recorded in
[current-system.md](current-system.md). Application files match; this does not
prove dependency-version parity between the Python 3.14 test venv and Python 3.12
production venv. No live failure injection occurred.

The immediately preceding feature verification at this source passed 604 tests,
20 migration tests and 63 browser cases, recorded in
[photo-inbox-2026-11-08.md](../../verification/photo-inbox-2026-11-08.md).
Those checks remain relevant controls; C1–C4 demonstrate gaps they did not cover.
This documentation-only review did not rerun that suite merely to repeat its pass.
Existing database, vault, nightly and printer tests were inspected to distinguish
tested happy paths from new adversarial scenarios.

## Coverage and exclusions

All seven review lenses are represented: purpose/boundaries (candidate table),
failure/recovery (F1–F6/S1–S10), authority/contracts (O1–O8/C1/C2), workload limits
(C4 and observed small inventory), trust/access (C3/C6 and role/front traces),
operability (C5/C6/backup observations), change/reversal (migration stages).

Excluded: real cloud quality benchmarking, physical printer tests, actual
power-cut tests, fleet reconfiguration, offline key-custody audit, destructive
restore/cutover, production load injection and Engram runtime changes. They are
not needed to choose the next repairs; explicitly required later checks remain
in the work items. No structural model checker or formal protocol proof was run.

Changes to creation identity, image preparation, transport, limits, physical-effect
records or job routing invalidate the corresponding claims/probes. Keep unaffected
evidence; rerun the affected scenarios against each implementation commit and its
installed package. New migration formats additionally require old-reader and
restore tests. Reviews and collected replies are not acceptance evidence.
