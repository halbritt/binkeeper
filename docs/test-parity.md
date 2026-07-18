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
| Ported | 285 | Direct standalone tests cover the domain plus local photo, vision, catalog, management, media, registration, and label paths. |
| Replaced | 1 | Stable explicit idempotency keys are covered through the standalone `EventIdentity` value interface instead of Engram's eight-parameter helper. |
| Deferred | 9 | Seven search-adapter nodes remain in BINK-8. Two capture-helper nodes remain gated with the live route transition rather than creating a second writer. |
| Engram compatibility | 0 | Compatibility shims and their contract tests start in later work items. |
| **Total** | **295** | Every collected baseline node id appears exactly once. |

## Residual risk

The BINK-6 and BINK-7 rows now run directly in this package, including their
disposable PostgreSQL and synthetic media paths. Vision remains advisory,
physical printing is faked, and catalog reads use the serving role. Owned
lexical search and the offline liveness adapter remain in BINK-8. The two old
capture-helper nodes stay deferred because exposing their writer before the
one-writer cutover would violate the extraction contract. This parity evidence
does not authorize a writer, hardware, or route change.
