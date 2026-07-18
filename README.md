# BinKeeper

BinKeeper is a local-first system for tracking physical storage bins, their
contents, locations, photos, movement history, and placement recommendations.

Status: the standalone package now owns the physical-inventory domain, CLI,
MCP schemas, forward migrations, evidence transfer, encrypted media path, and
owner catalog/management implementation, and exact/lexical inventory search.
The standalone loopback service, hardened deployment files, frozen tailnet
endpoint, authenticated backup/restore drill, and freshness gate are
implemented; production enablement and cutover remain gated. The working owner
service and live data authority still live in
[halbritt/engram](https://github.com/halbritt/engram). Do not deploy this
repository or treat it as the data authority until the verified one-writer
cutover in `BINK-11` succeeds.

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

- Owner data stays on the local machine unless the owner explicitly exports it.
- Raw captures, moves, observations, receipts, and owner decisions are
  append-only.
- Current location and other current state are folds over evidence, not mutable
  truth cells.
- Derived passports, confidence, routes, and manifests are rebuildable.
- Vision output is advisory. It cannot move a bin, register a bin, or accept a
  placement without an explicit owner action.
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

The standalone entry points are `binkeeper` and `binkeeper-mcp-stdio`. They
require an explicit local `BINKEEPER_DATABASE_URL`; neither imports the Engram
package or reaches its database. The web app factories are
`binkeeper.bin_catalog_web:create_app` and
`binkeeper.bin_photo_web:create_app`. The composed owner service is
`binkeeper-serve`, frozen to `127.0.0.1:8766` and documented in
[docs/deployment.md](docs/deployment.md); enabling it as the production writer
remains gated to BINK-11.
The packaged service is fail-closed for writes unless
`BINKEEPER_WRITES_ENABLED=1`; opening that gate is an owner-approved cutover
action, not a deployment default. The same process-wide gate protects the CLI
and MCP mutation paths, so compatibility subprocesses cannot bypass the frozen
HTTP writer.
Durability operations and exact recovery stop conditions are documented in
[docs/runbooks/backup-restore.md](docs/runbooks/backup-restore.md). The cutover
and compatibility-retirement procedures remain authority-gated runbooks, not
permission to change the live writer.
Run local inventory search with `binkeeper bin-search QUERY`. Transcript
liveness is optional and accepts only the inert file contract documented in
[docs/liveness-adapter.md](docs/liveness-adapter.md); absence is reported as
unavailable and never blocks core search.

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
