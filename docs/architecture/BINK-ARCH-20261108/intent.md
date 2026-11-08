# Intent, scenarios and assumptions

Source: owner instructions and `ed8ae090aa021c9b61a0752eabbe8dc321ab8595`.
A3 derived the scenarios before reading implementation or any candidate. The
lead reconciled that result with source and runtime evidence; no numeric service
objectives were supplied by the owner.

## Obligations

| ID | Status | Obligation and source |
| --- | --- | --- |
| O1 | Explicit | Save photos now; later create a bin or attach photos to an existing bin. Preserve every selected photo. Owner workflow request; ADR 0007. |
| O2 | Explicit | Preserve supplied Label/Contents, infer only blanks, and keep model output advisory. Owner correction; README; ADR 0007. |
| O3 | Explicit | Original evidence and accepted decisions remain immutable, scoped and attributable; projections are rebuildable. AGENTS, ADR 0001. |
| O4 | Explicit | Location changes follow owner-authorized move evidence; preserve containment/trip semantics. AGENTS, ADRs 0001/0003. |
| O5 | Explicit | Printing follows reviewed owner intent; duplicate requests must not cause repeated physical effects. README/extraction preservation contract. |
| O6 | Explicit | Originals, coordinates, stores and printers stay local. Advisory export is restricted to a downscaled image and prompt; subscription-covered models must use their subscription route without API fallback. AGENTS and ADR 0004; the newer routing instruction governs the older nightly configuration. |
| O7 | Explicit | Nightly review proposes possible contents additions only; it cannot rename bins or infer removal from absence. Owner correction in ADR 0006 and BINK-47. |
| O8 | Explicit | Tailnet HTTPS remains the owner entry path; BinKeeper remains the sole inventory data authority. No Engram writer restoration. AGENTS, ADR 0001, extraction retirement. |
| O9 | Inferred | Recovering from a failed confirmation should retain selected photos and entered text. This follows the requested avoidance of reupload/retyping; it does not establish a cross-browser draft service requirement. |
| O10 | Unknown | Acceptable latency, overlapping request count, RPO/RTO, unattended review backlog and retention horizons. Historical benchmarks and today's small inventory do not define these. |

Lead priority order: O1–O8 preservation and truthful outcomes, then responsive
recovery, then low operating/change cost. This order is inferred from the owner's
corrections and repository contracts; no new numerical threshold is implied.

## Initial scenarios and resolving checks

| ID | Event sequence | Obligation / causal mechanism | Smallest check and disposition |
| --- | --- | --- | --- |
| S1 | Save three photos, close, later create with label supplied | O1/O2: durable originals and complete selected set | Existing all-photo tests plus new creation collision probe C1; normal fresh-code case works, collision does not |
| S2 | Save, model fails or stalls, enter contents manually | O1/O2/O9: assistance must not own evidence or text | Existing model/vault tests; C7 source check separates recoverable photos from ephemeral draft |
| S3 | Duplicate or differently populated confirmation, then a delayed retry | O3/O5: identity must name owner intent | C1/C2 real-DB probes, including midnight and an intervening move; violations reproduced |
| S4 | Close/cancel after save but before analysis returns | O1/O2: cancellation cannot authorize inventory changes | Source confirms originals commit first; completion after disconnect and draft recovery remain unmeasured |
| S5 | Selected-photo operation fails halfway; registration commits before printer response is lost | O1/O3/O5: database atomicity versus external effects | Existing extra-photo rollback test; C5 source trace. Printer process-crash experiment is required by follow-up work |
| S6 | Owner is absent while nightly review sees additions, label mismatch and absent contents | O7: observations must remain proposals | Existing contents-only tests inspected; current failed job observed. No new live model execution |
| S7 | Two large photo requests overlap slow models/nightly work | O1/O10: admission and accepted save must be distinguishable | C4 synthetic ASGI experiment proves six simultaneous calls and 13-photo acceptance; no capacity/SLO claim |
| S8 | Old browser submits after upgrade/restart; then replays | O3/O5/O9: version and intent survive rollout | Migration requires explicit treatment of old confirmation forms and preserved new receipts; not yet implemented/tested |
| S9 | Decode/preparation fails before cloud inference; subscription route is unavailable | O6: fail closed at export and route selection | C3 transport spies reproduce original-byte fallback; C6 pin/route inspection. No real cloud request |
| S10 | Owner reviews a proposal, then records a real move via HTTPS | O4/O8: advisory and physical authority remain separate | Source folds and fronted GETs checked; no live mutation performed |

## Assumptions rechecked

| Assumption | State / observation | Revisit condition and owner |
| --- | --- | --- |
| Single owner, one local authority is an appropriate operating model | Supported by current instructions; source/installed package correspondence checked 2026-11-08 | Owner introduces shared/offline writers or an independent release/availability requirement |
| Originals persist before interactive inference | Supported by source and preceding feature tests; file power-loss durability is a separate unverified claim | BinKeeper maintainer changes upload/vault/worker path; run C8 experiment first |
| Supplied fields are preserved | Supported within proposal/error-return flow; confirmation error branch omits context | W2 must test all failure responses; durable cross-browser resume is a separate product decision |
| Every reviewed action has a stable request identity | Contradicted for photo creation by C1/C2; quick-label/manage have stronger identities | W2 completion requires delayed replay, payload conflict and concurrent allocation checks |
| Cloud bytes always meet the export contract | Contradicted by C3 at the request boundary | W1 completion requires zero transport calls after preparation failure across both clients |
| Existing nightly route is usable and authorized | Contradicted operationally by missing local slot; configured Opus/OpenRouter conflicts with current subscription routing instruction | W4 establishes permitted route and availability before any model run |
| Recent repair benchmark establishes adequate latency | Unverified; previous 6.633 s one-photo live check is a sample, not a bound | W5 measures phases/overlap and defines the operating budget before promising latency |
| Backups are currently usable | Supported in a limited window: latest backup/restore units exited successfully, fresh authenticated backup readiness passed | Every new durable format/storage policy needs restore and older-reader checks; offline key custody was not inspected |
| A durable job queue is needed | Unverified; independent proposal withdrew this as an immediate change after source inspection | Owner requires finish-after-navigation/restart, or measured abandonment/retry load justifies persistent jobs |

The selected Cairn nightly preference `fdb2deef-ff9a-48be-b694-84d692df40f5`
version 2 was checked against ADR 0006 and current contents-only code. It remains
applicable. The earlier independent proposal's commit-order wording was corrected:
location follows ledger `seq`, which is not a transaction commit-order guarantee.
