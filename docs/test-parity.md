# Engram BinKeeper test parity

This inventory accounts for the 295 direct BinKeeper tests collected from
Engram `a4b2b489c80370256d82256527428bd2b7714d6a` with:

```text
ENGRAM_TEST_DATABASE_URL=postgresql:///engram_test \
  .venv/bin/python -m pytest --collect-only -q tests/test_bin*.py
```

The row-level inventory is [test-parity.csv](test-parity.csv). The CSV's
`residual_risk` prose is the pre-cutover snapshot; the `BINK-11` cutover
(2026-07-18) has since closed the conditions it references.

| Classification | Count | Meaning |
|---|---:|---|
| Ported | 292 | Direct standalone tests cover the domain, owner workflows, lexical search, and offline liveness adapter. |
| Replaced | 1 | Stable explicit idempotency keys are covered through the standalone `EventIdentity` value interface instead of Engram's eight-parameter helper. |
| Deferred | 2 | The old capture-helper nodes target a retired Engram route; equivalent operator capture is the standalone photo drop (`tests/test_bin_photo_web.py`). |
| Engram compatibility | 0 | No baseline test was classified as a compatibility test; the shims were built under `BINK-9` and retired under `BINK-13`. |
| **Total** | **295** | Every collected baseline node id appears exactly once. |

## Residual risk

The BINK-6 and BINK-7 rows now run directly in this package, including their
disposable PostgreSQL and synthetic media paths. Vision remains advisory,
physical printing is faked, and catalog reads use the serving role. Owned
exact/lexical search and the offline liveness adapter are now standalone. The
two old capture-helper nodes were never ported: they targeted an Engram
operator capture route that was retired under `BINK-13`, and the standalone
photo-drop surface (`bin_photo_web`, `tests/test_bin_photo_web.py`) is its
replacement. This parity evidence does not authorize a writer, hardware, or
route change.

## Surface parity (BINK-35)

As of BINK-35, `bin-route`, `bin-placement-decision`, `bin-sweep`, and the
BINK-24 `bin-stash-route` batch router joined the original five MCP tools;
`bin_containment`, `bin_stash_run`, `bin_virtual_define`, and
`bin_virtual_list` followed, for thirteen tools today. Two commands are
deliberately CLI-only: `bin-anchor-label` (printer intent) and
`bin-ocr-harvest` (the nightly true-up entry point). Mutation tools share the
CLI's fail-closed writer gate (`mcp.call_tool` delegates to `cli.execute`);
the rest run read-only. The schema snapshot test pins the tool list.
