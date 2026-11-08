# Candidates and adjudication

All candidates target source `ed8ae090aa021c9b61a0752eabbe8dc321ab8595`.
The independent first proposal was frozen before current-system exposure in
[independent-first.md](independent-first.md). A4 then compared it with source
without seeing A2's proposal. The lead's decision below uses the subsequent
independent verification, not the number of agreeing reviewers.

## B0 — retain behavior and document its limits

Keep the local application, evidence folds, saved-photo recovery and request-time
analysis. Accept ephemeral form state, manual printer inspection and existing
scheduled work. This has the lowest change burden and remains adequate where no
failure pressure has been demonstrated. It does not satisfy O5/O6 under C1–C3:
creation identity and export refusal are concrete defects, not optional polish.

## I1 — independent first design

A1 proposed the same general local topology, plus durable photo batches, drafts,
persisted advisory jobs and physical-action accounting. It kept accepted inventory
separate from operational state and rejected network services/brokers. Its five
claims and every significant boundary are preserved in the immutable first record.

After source inspection, A4 withdrew most additions. Independent photo storage,
scoped reuse, owner field precedence, ordinary all-photo atomicity and evidence
folds already exist. A4 recommends preserving full context on confirmation errors,
then treating draft persistence separately from analysis persistence. It withdrew
its provisional 500 ms target and corrected location ordering to ledger `seq`.
It found no evidence requiring a durable interactive job queue today.

## E1 — strongest practical evolution

A2 proposes three concrete in-process responsibilities: cloud export normalization,
reviewed creation identity/transaction, and physical print intent/outcome accounting.
It preserves saved attachment's existing scoped lock and stable bin/photo identity.
It would measure shared inference admission and vault recovery before introducing
more scheduling/storage structure. Its initial concerns were checked as C1–C4;
printer and vault fault claims remain bounded source observations C5/C8.

## Selected direction D1

Retain the current local application, owned PostgreSQL ledgers, encrypted vault,
read-time projections, tailnet front and local printer/fleet adapters. Strengthen
the existing boundaries in small semantic repair slices:

1. Refuse cloud transport unless image normalization succeeds and meets its bound.
2. Give **create a new bin** an explicit stable owner-action identity and atomic
   code allocation/conflict handling. Keep **register this existing physical
   label/place** a distinct operation; do not rewrite its history or pretend that
   a calendar-day placement identity is a creation-request identity.
3. Give every print entry point durable intent and observed outcome accounting,
   preserving the rule against automatically retrying an uncertain physical effect.
4. Reconcile the nightly subscription route and exact local model dependency,
   and expose advisory failure/freshness separately from core readiness.
5. Apply a consistent upload limit and verify a shared inference budget/error path.
   Measure phase latency and overlap before selecting broader job/draft machinery.
6. Run a bounded vault recovery experiment before declaring power-loss durability
   or implementing reconciliation. An orphan is not permission to delete evidence.

This is a recommendation under the architecture-review request. It does not mark
new runtime behavior deployed or owner-accepted. The review itself changes only
documentation/tracking and uses synthetic experiments.

## Boundary decisions and removal counterfactuals

| Boundary | Purpose / disposition | Removal or move counterfactual | Cost and displaced responsibility |
| --- | --- | --- | --- |
| B1 local authority and projections | Retain canonical evidence plus rebuildable folds | Mutable catalog authority or another writer loses provenance and deterministic recovery | Existing folds stay local; no cross-service reconciliation added |
| B2 encrypted original vault | Retain; investigate durability contract | Inline DB originals would move blob size/backup costs into the DB; remote storage changes locality authority | Current two-store recovery remains explicit; C8 resolves crash ordering before changes |
| B3 trusted owner front and serving role | Retain; document MCP read-role exception | Removing guards exposes owner actions; separate auth service has no established driver | Local CLI trust stays local; least-privilege cleanup is lower priority than verified workflow defects |
| B4 cloud byte preparation | Strengthen at the common transport seam | Caller-only validation leaves other callers able to export original bytes | Small normalization/refusal contract; permitted model failure replaces unauthorized fallback |
| B5 reviewed creation command | Alter in-process; own action identity, payload comparison, code and all links | Leaving coordination in adapters retains calendar identity confusion; separate service adds a transaction/network boundary | A concrete function/module is sufficient; no generic command bus, new database or distributed transaction |
| B6 physical printer effect | Unify intent and append-only observations | Hiding I/O in registration loses recoverable explanation; retrying a queue can duplicate paper | Small durable receipt and operator reconciliation; exactly-once paper is not promised |
| B7 advisory execution | Retain request-time interaction and local nightly jobs initially | Merging assistance with evidence acceptance makes model availability authoritative; a new service moves rather than removes admission/retry problems | Consistent limits and visible errors first; durable ownership/cancellation machinery only if required |
| B8 backup/restore | Retain independent authenticated artifacts and disposable drills | Removing recovery boundary loses tested restore; another online mirror is not equivalent to backup | New formats must enter the existing manifest/restore contract; offline key custody remains owner's responsibility |
| B9 draft persistence | Defer | Request-only forms lose unfinished text after navigation; browser or server drafts would each add retention/conflict/privacy rules | Preserve returned error context now; add drafts only for an explicit resume promise or measured abandonment |

## Disagreement and limits

A4's statement that all selected photos are already atomic is supported for a
fresh, non-conflicting creation. C1 shows why that does not establish correct
ownership when a suggested code is reused. The lead therefore selects a narrow
creation command, while agreeing with A4 that a general command framework is
unearned. This is a scenario/applicability correction, not a source revision change.

A2 raised capacity and power-loss risks. C4 establishes six admitted model calls
and an inconsistent batch bound; it does not establish a production saturation
incident. C8 remains inconclusive about actual power-loss behavior. Those limits
prevent a claim that another service, queue or database is necessary.

No candidate may change location ordering, infer missing-item removal, restore an
Engram writer, expand cloud export, or switch a subscription-covered model to an
API route. Drafts and jobs must never become alternative inventory authority.

Reopen topology if independent deployment/isolation becomes a demonstrated need,
if measured single-process limits persist after bounded admission, or if accepted
offline/multi-writer requirements appear. Reopen persistent jobs only for a defined
finish-after-navigation/restart guarantee or measured duplicated/abandoned work.
