# BinKeeper

BinKeeper is a local-first system for tracking physical storage bins, their
contents, locations, photos, movement history, and placement recommendations.

Status: repository and extraction planning only. The working implementation
still lives in [halbritt/engram](https://github.com/halbritt/engram). Do not
deploy this repository or treat it as the data authority until the cutover work
in the BinKeeper Plane project is complete.

The extraction plan is in
[docs/extraction-analysis.md](docs/extraction-analysis.md). Work is tracked in
the private Proximal Plane workspace under project `BINK`.

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
