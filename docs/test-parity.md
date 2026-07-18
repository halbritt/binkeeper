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
| Ported | 133 | Direct domain and persistence tests now cover location, confidence, presence, orders, sweep, passports, volume, routing, placement receipts, and feedback. |
| Replaced | 1 | Stable explicit idempotency keys are covered through the standalone `EventIdentity` value interface instead of Engram's eight-parameter helper. |
| Deferred | 161 | Owner web/media, search-adapter, and deployment behavior remains assigned to later extraction items. |
| Engram compatibility | 0 | Compatibility shims and their contract tests start in later work items. |
| **Total** | **295** | Every collected baseline node id appears exactly once. |

## Residual risk

The BINK-6 rows now run directly in this package, including their disposable
PostgreSQL paths. Photos, printing, vision, catalog and management flows,
owned lexical search, the offline liveness adapter, and tailnet-fronted
deployment remain deferred to BINK-7 through BINK-10. This parity evidence does
not authorize a writer or route change.
