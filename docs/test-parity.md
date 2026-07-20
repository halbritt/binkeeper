# Engram BinKeeper test parity

This inventory accounts for the 295 direct BinKeeper tests collected from
Engram `a4b2b489c80370256d82256527428bd2b7714d6a` with:

```text
ENGRAM_TEST_DATABASE_URL=postgresql:///engram_test \
  .venv/bin/python -m pytest --collect-only -q tests/test_bin*.py
```

The row-level inventory is [test-parity.csv](test-parity.csv).

| Classification | Count | Meaning |
|---|---:|---|
| Ported | 292 | Direct standalone tests cover the domain, owner workflows, lexical search, and offline liveness adapter. |
| Replaced | 1 | Stable explicit idempotency keys are covered through the standalone `EventIdentity` value interface instead of Engram's eight-parameter helper. |
| Deferred | 2 | The old capture-helper nodes remain gated with the live route transition rather than creating a second writer. |
| Engram compatibility | 0 | Compatibility shims and their contract tests start in later work items. |
| **Total** | **295** | Every collected baseline node id appears exactly once. |

## Residual risk

The BINK-6 and BINK-7 rows now run directly in this package, including their
disposable PostgreSQL and synthetic media paths. Vision remains advisory,
physical printing is faked, and catalog reads use the serving role. Owned
exact/lexical search and the offline liveness adapter are now standalone. The
two old capture-helper nodes stay deferred because exposing their writer before the
one-writer cutover would violate the extraction contract. This parity evidence
does not authorize a writer, hardware, or route change.

## Surface parity (BINK-35)

Every CLI command now has an MCP counterpart: `bin-route`,
`bin-placement-decision`, `bin-sweep`, and the BINK-24 `bin-stash-route`
batch router joined the original five tools. Mutation paths (`trip_scan`,
`bin_placement_decision`) share the same fail-closed writer gate as the CLI;
the rest run read-only. The schema snapshot test pins the tool list.
