# BinKeeper

BinKeeper is a system for tracking physical storage bins, their
contents, locations, photos, movement history, and placement recommendations.

Status: the standalone package now owns the physical-inventory domain, CLI,
MCP schemas, forward migrations, evidence transfer, encrypted media path, and
owner catalog/management implementation, and exact/lexical inventory search.
The standalone loopback service, hardened deployment files, frozen tailnet
endpoint, authenticated backup/restore drill, and freshness gate are deployed.
The owner-approved `BINK-11` one-writer cutover completed on 2026-07-18;
BinKeeper is now the live physical-inventory authority. Engram retains frozen
historical evidence and migrations, but its runtime, compatibility names, and
direct inventory writer were retired under `BINK-13` after the accepted
consumer and rollback-window gates. The advisory vision lane with its
benchmark-selected cloud default (ADR 0005) and the nightly local-only
peripheral-OCR true-up timer (`binkeeper-ocr-harvest.timer`) are also
deployed. ADR 0006's input-keyed Opus 5 + local Qwen3-VL 8B label-drift pass,
rebuildable owner review queue, and append-only accept/dismiss workflow are
deployed as `binkeeper-label-drift.timer` after the OCR pass.

The whole-system design is described in
[docs/architecture.md](docs/architecture.md).

The extraction plan is in
[docs/extraction-analysis.md](docs/extraction-analysis.md). Work is tracked in
the private Proximal Plane workspace under project `BINK`.

Architecture decisions use short, sequential records under
[`docs/adr/`](docs/adr/). The accepted standalone-authority decision is
[ADR 0001](docs/adr/0001-standalone-authority.md).

The [test parity inventory](docs/test-parity.md) accounts for the 295-test
Engram baseline and keeps unported behavior explicitly deferred.
[ADR 0002](docs/adr/0002-blob-key-transition.md) fixes the blob transition as
re-encryption under a BinKeeper-owned key.
The [transfer contract](docs/transfer.md) describes the read-only exporter,
isolated importer, and fail-closed manifest.
It also stages the authority-cutover blob envelopes under a distinct
BinKeeper-owned key while retaining both source and target manifests.

## Invariants

- Owner data stays on the local machine unless the owner explicitly exports
  it. One standing owner-approved export exists ([ADR 0004](docs/adr/0004-cloud-vision-backend.md)):
  the advisory vision lane may send the downscaled inference JPEG and its
  prompt text to the configured cloud vision provider. Nothing else leaves.
  The default provider inside that unchanged scope is benchmark-selected in
  [ADR 0005](docs/adr/0005-benchmarked-vision-default.md). The deployed
  [ADR 0006](docs/adr/0006-label-drift-review-queue.md) nightly ensemble also
  sends that bounded inference input through OpenRouter to Anthropic.
- Raw captures, moves, observations, receipts, and owner decisions are
  append-only.
- Current location and other current state are folds over evidence, not mutable
  truth cells.
- Physical bin containment is a single-parent, acyclic fold over pack and
  unpack evidence. A contained bin moves with its outermost container.
- Derived passports, confidence, routes, and manifests are rebuildable.
- Vision output is advisory. It cannot move a bin, register a bin, or accept a
  placement without an explicit owner action. This holds for every vision
  backend, local or cloud.
- Owner web surfaces must work through tailnet HTTPS; loopback-only success is
  not sufficient.

## Development

```sh
make install
make lint
make typecheck
make test
make migration-test
make package-test
```

The primary entry points are `binkeeper` and `binkeeper-mcp-stdio`; both
require an explicit local `BINKEEPER_DATABASE_URL`, and neither imports the
Engram package or reaches its database. The package also installs
`binkeeper-serve` (same database requirement), `binkeeper-migrate`,
`binkeeper-transfer` ([docs/transfer.md](docs/transfer.md)),
`binkeeper-backup`, and `binkeeper-restore-drill`
([docs/runbooks/backup-restore.md](docs/runbooks/backup-restore.md)). The web app factories are
`binkeeper.bin_catalog_web:create_app` and
`binkeeper.bin_photo_web:create_app`. The composed owner service is
`binkeeper-serve`, frozen to `127.0.0.1:8766` and documented in
[docs/deployment.md](docs/deployment.md). It is the production writer after the
accepted `BINK-11` cutover.
The packaged service is fail-closed for writes unless
`BINKEEPER_WRITES_ENABLED=1`; opening that gate is an owner-approved cutover
action, not a deployment default. The same process-wide gate protects the CLI
and MCP mutation paths.
Durability operations and exact recovery stop conditions are documented in
[docs/runbooks/backup-restore.md](docs/runbooks/backup-restore.md). The completed
compatibility-retirement record is in
[docs/runbooks/compatibility-retirement.md](docs/runbooks/compatibility-retirement.md).
Run local inventory search with `binkeeper bin-search QUERY`. Transcript
liveness is optional and accepts only the inert file contract documented in
[docs/liveness-adapter.md](docs/liveness-adapter.md); absence is reported as
unavailable and never blocks core search.

Record physical nesting with
`binkeeper bin-containment --action pack --bin BIN --container CONTAINER --idempotency-key KEY`
and reverse it with the `unpack` action. Both bins must have the same known site
before packing. A contained bin must be unpacked before it can be moved
directly. The owner management page exposes the same workflow under
**Bin inside a bin**. [ADR 0003](docs/adr/0003-physical-bin-containment.md)
records the evidence and projection contract.

A nightly peripheral-OCR true-up (`binkeeper bin-ocr-harvest --local-only`,
deployed as `binkeeper-ocr-harvest.timer` from `deploy/systemd/`) re-reads
geolocated vault photos with the local model only and records corroborative
`peripheral_ocr` location observations. It is idempotent, never relocates a
bin, and fails closed: exit 3 means no geofence site is configured, exit 4
means photos were read but no code was legible anywhere. See
[docs/deployment.md](docs/deployment.md).

The follow-on label-drift pass runs at 04:00 through an exact-model gpu-fleet
lease. It fans each changed bin photo out concurrently to OpenRouter Anthropic
Opus 5 and local peecee `qwen3-vl:8b`, stores the union as append-only advisory
evidence, and queues only material diffs. The catalog shows the rebuildable
pending count; the manage page lets the owner edit and save a normal profile
snapshot or append a proposal-linked dismissal. It never changes a label by
itself. See [ADR 0006](docs/adr/0006-label-drift-review-queue.md) and
[docs/deployment.md](docs/deployment.md).

`make check` runs the full verification sequence; the editable install is a
prerequisite of each target. PostgreSQL acceptance tests require a disposable
database supplied as `BINKEEPER_TEST_DATABASE_URL`; for example:

```sh
createdb --template=template0 binkeeper_disposable
BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_disposable \
  .venv/bin/pytest -q -m migration tests/test_persistence.py
dropdb --force binkeeper_disposable
```

The suite is synthetic and never connects to Engram. `binkeeper-migrate`
applies checksum-pinned migrations to an explicit URL or
`BINKEEPER_DATABASE_URL`; do not point it at an Engram database.
