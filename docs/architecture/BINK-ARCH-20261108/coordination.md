# Coordination and source index

Lead and integration owner: agent-1315 (`/root`), observed gpt-6-astra,
Codex-3 ChatGPT subscription. Review tracker: BINK-53. Canonical source revision:
`ed8ae090aa021c9b61a0752eabbe8dc321ab8595`. Workers inspect immutable source;
only the lead edits tracked files. No worker edits, worktrees or deployments.

## Roster and assignments

| ID | Actual worker / mode | Roles and selected input | Output / state |
| --- | --- | --- | --- |
| A1 | `/root/independent_architect`, fresh built-in context | Independent architect; intent-only briefing, excluded topology/ADRs/source/proposals | `independent-first.md`, returned and frozen before A4 |
| A2 | `/root/current_system`, fresh built-in context | Investigator/evolution architect; source ed8ae09, binding repo docs, no other proposals | Current flows and evolution findings accepted after lead source/runtime checks and A5/A6 probes; closed |
| A3 | `/root/intent_challenger`, fresh built-in context | Intent/challenger; intent-derived scenarios first, then source ed8ae09 | Scenarios delivered before source/proposals; intent/source findings incorporated with C1/C3 verification; closed |
| A4 | continuation of A1 | Reconcile independent-first SHA256 `75ba0c8fcf14114ae05d4203d12f89ccd40e4188c8124a48186d7ddd59c4d054` with ed8ae09; other proposals still excluded | Revised position accepted with the C1 applicability correction; closed |
| A5 | `/root/verify_creation`, fresh built-in context | Verify create collision and cross-midnight replay, exact ed8ae09 and scenario; disposable DB only | C1/C2 real transaction/replay evidence accepted with stated limits; closed |
| A6 | `/root/verify_export`, fresh built-in context | Verify cloud encoder refusal/export and bounded request behavior, exact ed8ae09; fake transport only | C3/C4 evidence accepted, absolute unboundedness claim rejected; closed |

All workers request inferred Sol/high: live Codex catalog resolves this to
`gpt-6.1-sol` with supported high effort. The requested model and returned built-in
agent identity are recorded; no separate native provider attestation or Cairn
identity is invented. No nested delegation. Individual discovery assignments are
bounded to one pass (about 6–10 minutes); verification to two focused experiments
(about 10 minutes). Deadlines are supervised, not launcher-enforced. Lead accepts
evidence separately from a worker's completed status.

## Route, capacity and coordination limits

`codex login status` reports ChatGPT authentication; the inherited route retains
`CODEX_HOME=/home/halbritt/.codex-3`. No OpenRouter or API fallback is selected.
Native floating Opus and Argon were considered; known inherited headroom and a
fresh-context tool avoid a separate native bootstrap/admission lifecycle. This
reduces worker-model diversity; lead and workers have different classes, not
independent subscriptions.

Quota source: documented local collector at `127.0.0.1:8796`; the HTTPS collector
returned 403. Initial accepted snapshot:
`quota-local-184715/snapshot.json`, SHA256
`b967049a93f9d30bfa76ef1c60909876bd5b2a5c8d17305c3e805278760f19df`,
under the private review directory. Codex-3 account/window were fresh, 92% remaining
in the known provider-pool window; this is not a token estimate or all-model
entitlement. Other constraints remain unknown. The verification-wave refresh
(`quota-wave2-185349/snapshot.json`, SHA256
`455ff7c083b7090400564f912d40772d3e04e625fbeb63040dc791b9072dd2df`)
again reported fresh known headroom of 92%, observed at 18:51:36Z.
Quartermaster installed catalog lacks Codex-3 and enrolls
other consumers; no adapter or capacity reservation is invented for built-in work.

Cairn client/server protocol 2 is compatible; `coordination-waits/1` is disabled.
The lead created topic `arch.bink-arch-20261108.recovery.r1` under its own identity
(subscription request `0cab171c-1fb8-4d76-93ef-78996ea2ded6`). Built-in workers have
no separate native inbox; selected assignments/results are bridged by the lead.
There is no native multi-recipient response group and no claimed child subscription.
Built-in messages provide immediate supervision. Selected Cairn records and this
index provide durable provenance. The lead removed its own subscription at
2026-11-08 19:15:59Z; the API returned `active: false` (request
`ed4b12e6-ad54-4159-b57f-17c4721219af`). No child subscriptions or response group
existed to close.

Raw quota payloads, experiments, runtime checks and coordination receipts remain
under `~/.local/state/binkeeper/architecture-review-20261108/`. No credentials,
owner images, coordinates, bin contents or raw conversation logs are retained here.

## Selected record and closure

Cairn record `7f7136df-2779-4afe-87c5-d06176481ecf`, version **1**, preserves the
recommendation, exact source revision, verified cases, limits and BINK-54..59
work identities. It is shared guidance, not a claim that the repairs are deployed.
The earlier owner preference `fdb2deef-ff9a-48be-b694-84d692df40f5`, version 2,
was checked against the current contents-only nightly contract.

All six assignments are closed. A5 removed its exact disposable database and
verified absence; A6 used in-memory/fake transports. No agent created a worktree,
native worker process, external model request or physical print. The lead retains
only selected private synthetic experiments/receipts and the tracked review.
Workers did not edit tracked files. Source implementation is unchanged from ed8ae09.
