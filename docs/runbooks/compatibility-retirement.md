# Compatibility-shim retirement runbook

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
