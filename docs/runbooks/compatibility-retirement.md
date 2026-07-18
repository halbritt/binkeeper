# Compatibility-shim retirement runbook

Status: completed under `BINK-13` on 2026-07-18. `BINK-12` consumer probes and
the full rollback-window observation passed before Engram's compatibility
environment, shims, embedded runtime, package data, and BinKeeper-specific
smokes were removed. Engram retains historical migrations and evidence only.

Execution evidence: Engram retirement commit `ec9ac02` and generated-build
cleanup `2b278f2` are pushed on `master`. The Engram suite passed with 1,372
tests and 923 skips; focused cross-surface tests passed with 134 tests and 9
skips; reference and documentation checks were clean. After the live restart,
Engram health remained HTTP 200 while `/bins/` and `/bin-photo/` returned 404,
its legacy CLI command was unavailable, and its seven MCP tools contained no
BinKeeper name. BinKeeper remained ready on loopback and tailnet HTTPS with
eight captures, five move events at watermark 5, and four evidence blobs.

The steps below are the preserved execution record, not a standing instruction
to restore compatibility.

Engram compatibility shims may be removed only after BINK-11 cutover acceptance
and BINK-12 Praxis consumer probes pass. They expire no later than 30 days after
cutover unless the owner records a new decision.

1. Inventory calls to legacy Engram web paths, CLI commands, and MCP tool names.
2. Prove Praxis and every named consumer use only `binkeeper.*` contracts and
   hold no Engram table or implementation imports.
3. Remove the compatibility environment from Engram and restart it. A legacy
   call must become explicitly unavailable; it must not regain table access.
4. Observe one accepted window, then remove shim code and obsolete Engram
   BinKeeper runtime wiring under BINK-13.
5. Supersede current Engram runtime decisions/docs without deleting historical
   migrations, captures, ledgers, blobs, decisions, or audit provenance.

Stop if any consumer still calls a legacy name, writes are disabled in the new
adapter, evidence watermarks diverge, or the rollback window is still open.
