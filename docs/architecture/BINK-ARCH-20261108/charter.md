# BINK-ARCH-20261108 — review charter

Lead: agent-1315, gpt-6-astra on the existing Codex subscription.
Source revision: ed8ae090aa021c9b61a0752eabbe8dc321ab8595, master.
Question: Which architectural changes, if any, best improve reliable and responsive
photo-to-inventory workflows while preserving local evidence, explicit owner
control, and manageable recovery?

Scope: BinKeeper browser/CLI/MCP; originals, canonical evidence and projections;
interactive and nightly inference; local model lease integration; physical
printing; tailnet access; backup, restore and deployment operation. External
systems are inspected only at BinKeeper's contracts. No external repo edits.

Authority: architecture review, read-only runtime metadata, synthetic disposable
experiments, documentation, Plane work items and trunk integration. Prior feature
repairs are complete. This review does not authorize new production migrations,
physical printing, model spending on owner photos or deployment changes.

Priorities: preserve originals/history and owner decisions first; recoverable,
responsive owner workflows second; low operator and migration burden third.
Numeric latency, throughput, recovery-time and recovery-point objectives are
unknown. Do not manufacture scale or service-level requirements.

Budget: three workers at once plus the lead; one discovery/proposal wave and one
focused challenge/verification wave, up to eight bounded assignments, no nested
delegation. Aim for a decision and executable work within about 45 minutes after
bootstrap. Stop expanding when more evidence cannot change the next action.

Outputs: docs/architecture/BINK-ARCH-20261108/{README,intent,current-system,
candidates,verification,migration,coordination}.md plus synthetic probes if useful.
Root owns all tracked files; workers return read-only findings. Initial proposals
and scenario sets are immutable snapshots. Every material claim has exact source
or executed-check evidence, a limit, and supported/refuted/inconclusive status.

Completion: one recommendation including baseline; refreshed assumptions;
critical flows and boundaries; initial independent proposal before cross-review;
resolved or explicitly conditional consequential objections; reversible migration
and ordered owned Plane work; selected Cairn records; clean pushed master.

Coordination: use inherited fresh-context Sol workers under the existing Codex-3
subscription. Native durable Cairn waits are disabled; built-in workers have no
separate Cairn inbox. The registered lead bridges selected task/result records,
owns supervision, and uses a scoped review topic without inventing native child
identities or claiming a response group. More worker-class diversity is deferred
because built-in execution supports prompt independent delivery without a native
session/bootstrap lifecycle. This is one subscription, not cross-subscription
execution. Codex catalog currently offers gpt-6.1-sol with high effort. Quota
observation at 18:47Z: Codex-3 92% on its fresh known provider-pool window;
other unreported constraints remain unknown. Quota endpoint HTTPS returned 403;
the documented local collector succeeded. No admission adapter is invoked by the
inherited-worker tool; do not reserve capacity or alter global configuration.
