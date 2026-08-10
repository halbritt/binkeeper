# Vision benchmark records

This directory is the durable, sanitized record of BinKeeper vision-model
benchmarks. Models and serving runtimes change, so each benchmark is a new
dated record rather than an edit to an old result.

Raw `results.json` files remain owner-local because they contain bin codes,
inventory labels, and per-photo predictions. A committed record may contain
only aggregate metrics, non-identifying protocol counts, model and runtime
provenance, limitations, and SHA-256 digests of the private evidence files.
Photos, photo hashes, bin codes, item labels, coordinates, credentials, and
raw model output do not belong here.

## Comparison contract

Every record names a protocol revision. Change that revision when the corpus,
prompt, scoring rules, repeat count, warmup policy, or measurement boundary
changes. Results from different protocol revisions are not direct standings.

For each candidate, record the provider model ID or exact artifact revision,
the serving-runtime revision, inference settings that affect results, and any
provider revision that was unavailable. A later quantization, provider alias,
runtime build, or prompt is a new candidate even when its display name is
unchanged.

Two-model results use union recall: an owner item is matched when either member
matches it on the same benchmark call. Parallel latency is modeled as the
slower member's wall time for each aligned call; it is not a measured
concurrent-serving result. The scorer rejects duplicate call identities,
requires photo hashes, and rejects different photo hashes or different
owner-evidence sets. Member errors and invalid JSON remain visible because a
theoretical recall union does not prove that a fail-closed production ensemble
would emit a proposal. Each record must separately state whether its two
members can actually serve concurrently on the available hardware; two local
models competing for one GPU may have only a quality-union result.

Benchmark evidence does not change a deployed model, accept a decision, or
authorize cloud export. Those transitions require their own accepted decision
and live verification.

## Records

| Date | Protocol | Candidate | Result | Status |
|---|---|---|---|---|
| [2026-08-10](vision/2026-08-10-muse-glimmer-30b-17g.md) | `vision-owner-photo-v1` | Muse Glimmer 30B 17G, solo and two-model unions | best Muse pair 0.764 recall; Opus 5 + local 8B control 0.832 | evidence only |
