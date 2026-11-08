# Doctrine provenance and claim limits

Repository contracts, accepted decisions and current owner instructions take
precedence. Pincite supplied an evidence discipline and candidate comparison;
it did not authorize implementation or establish system correctness.

The release retrieval gate passed against
`/home/halbritt/.local/share/pincite/release`, commit
`6deb1af41d41075b28577bcf2126c9db90d0e7c0`. Identities:

- Corpus: `corpus-2026-07-12-84d6cdfc0e9f`.
- Doctrine: `doctrine-2694f0450e78ff1e`.
- Retriever: `retriever-8b654e47fcb1be28`.
- Source fingerprint: `0e82bf7c3ddf08f986a5f52f95e358066d2292a3f8fb974d0fb4f7f0aa1c8e19`.
- Initial packet: `pkt-d16738bf7af246f1`, SHA256
  `d16738bf7af246f16a95fe10569bc42cdb2447a35edc3ce323acbe9af1adff7b`.
- Evidence-reassembled packet: `pkt-40e0807cb955cc35`, SHA256
  `40e0807cb955cc351f827a94681f4f6808e52dd48d8be1733ff79f0f0824dbc4`.

The question was: “Which changes to BinKeeper boundaries and recovery are
justified by its owner workflows and present failure evidence?” The route was
`coding-agent` / `architecture-assessment` / `architecture-review`, Python,
correctness risk. One evidence-gathering pass supplied eleven typed
observations: owner requirements, repository contracts, accepted ADRs, source
structure, runtime, operating constraints, two executed experiments, inspected
version history, and the candidate/migration artifacts. The last two establish
what was proposed, not that proposed improvements have been achieved. A second
assembly refreshed artifact hashes after the documentation accuracy pass; its
evidence claims and nine remaining obligations were unchanged.

The packet reports 61 of 70 obligations satisfied. The nine remaining generic
obligations are retained below. **No broader claim about topology fitness,
module quality, capacity or production reliability is made.** They are not
material to the narrower recommendation: repair the demonstrated contract
violations at existing seams and avoid a topology change without a demonstrated
driver. They would become material before approving the broader claims named.

| Missing requirement | Classification and rationale for this recommendation |
| --- | --- |
| current navigation and coordination cost | Nonmaterial: no source reorganization or maintainability improvement is selected or quantified. |
| representative change and co-change paths | Nonmaterial: three relevant changes were inspected, but no representative repository-wide co-change analysis was run. It is unnecessary for the reproduced semantic defects. |
| visibility and enforcement capability | Nonmaterial: no new source partition or claim of mechanically enforced module isolation is selected. |
| acceptable shared deployment and failure domain | Nonmaterial to retaining the baseline while making narrow repairs. No availability objective or proof of acceptable outage impact exists; require this before promising deployment fitness or rejecting an explicit isolation requirement. |
| cohesive module responsibilities | Nonmaterial: F1–F6 map concrete responsibilities, but this is not a comprehensive cohesion assessment or certification. |
| enforceable source boundaries | Nonmaterial: in-process seams are observed, but compiler/import enforcement was not established. No claim that current boundaries are fully enforced. |
| indispensable features ranked by business impact | Nonmaterial: the lead's preservation-first priorities are inferred and labeled. The first repairs restore explicit export and owner-intent contracts; they do not depend on an invented business-impact ranking. |
| incidents | Nonmaterial: current failure observations and synthetic reproductions are sufficient for these repairs. No historical incident-frequency or production-impact claim is made. |
| expected future change from accepted plans | Nonmaterial: the review does not speculate about an unaccepted growth roadmap. New shared/offline writers or isolation needs reopen the topology question. |

Selected doctrine used in the result: `universal-evidence-before-intervention`,
`universal-no-change-option`, `architecture-earned-boundary`,
`operations-designed-failure-modes`, `universal-repository-contract-precedence`,
`universal-separate-semantic-structural-change`,
`universal-preserve-behavior-by-default`, `agent-conduct-authority-bounded-action`
and `refactoring-change-type-classification`. The recommendation does not rely on
a claim that every modular-monolith fitness obligation is met.

The no-change baseline, narrow repairs and deferred draft/job alternative are in
[candidates.md](candidates.md). Preservation and semantic deltas, acceptance
criteria and reversal limits are in [migration.md](migration.md). Source locators
and experiment hashes are in [verification.md](verification.md) and
[probe-manifest.json](probe-manifest.json). Material uncertainty about physical
crashes, power loss and interrupted drafts becomes the specified experiments;
the review abstains from declaring those behaviors correct.

Actual authority is the review and its documentation/tracking/integration, despite
the packet's generic `execute` ceiling. No new service, owner-data migration,
provider route, real model run or physical print was authorized or performed by
this assessment. Reopen when source or operating assumptions change, when an
implementation alters a verified seam, or when a new recovery/isolation promise
would require currently missing evidence. Stop if a proposed action expands data
export, changes canonical authority or requires an unapproved external effect.

A `decision-receipt/2` with typed observations and explicit recommendation status
is retained privately at
`~/.local/state/binkeeper/architecture-review-20261108/decision-receipt.json`.
Consumption classification is retained beside it as `doctrine-citations.json`;
the trace is under `~/.local/share/pincite/trace`. Temporary retrieval packets
and evidence working files are removed after closure.
