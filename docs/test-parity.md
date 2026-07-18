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
| Ported | 5 | The pure location fold and trip reconciliation behaviors run in this repository with deterministic synthetic events. |
| Replaced | 1 | Stable explicit idempotency keys are covered through the standalone `EventIdentity` value interface instead of Engram's eight-parameter helper. |
| Deferred | 289 | Engram remains authoritative; the row names its extraction work item. |
| Engram compatibility | 0 | Compatibility shims and their contract tests start in later work items. |
| **Total** | **295** | Every collected baseline node id appears exactly once. |

## Residual risk

The ported tests protect only the pure, in-memory core of the move ledger.
Database constraints and roles, capture storage, photos, printing, vision,
catalog and management flows, placement, search, CLI/MCP dispatch, and
tailnet-fronted behavior still execute only in Engram. Their rows stay
`deferred` until the named work items port or replace them and record new
verification evidence. A passing BinKeeper scaffold suite is not parity for
those behaviors and does not authorize a writer or route change.
