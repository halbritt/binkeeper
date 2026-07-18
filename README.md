# BinKeeper

BinKeeper is a local-first system for tracking physical storage bins, their
contents, locations, photos, movement history, and placement recommendations.

Status: the standalone package, pure preservation tests, BinKeeper-owned
forward migrations, and synthetic encrypted-blob path are present. Owner
surfaces, deployment, data import, and cutover remain in progress. The working
implementation and live data authority still live in
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

`make check` runs the full verification sequence; the editable install is a
prerequisite of each target. PostgreSQL acceptance tests require a disposable
database supplied as `BINKEEPER_TEST_DATABASE_URL`; for example:

```sh
createdb binkeeper_disposable
BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_disposable \
  .venv/bin/pytest -q -m migration tests/test_persistence.py
dropdb --force binkeeper_disposable
```

The suite is synthetic and never connects to Engram. `binkeeper-migrate`
applies checksum-pinned migrations to an explicit URL or
`BINKEEPER_DATABASE_URL`; do not point it at an Engram database.
