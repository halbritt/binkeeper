# Muse Glimmer 30B 17G vision benchmark

- Record ID: `vision-20260810-muse-glimmer-30b-17g-v1`
- Protocol: `vision-owner-photo-v1`
- Candidate run: 2026-08-10
- Companion runs: 2026-08-06
- Status: benchmark evidence; no model-selection or deployment decision

## Protocol

Muse ran through BinKeeper's production `VisionClient.analyze` seam with the
production prompt over the same private six-photo owner corpus used for ADR
0005. Each model received one unscored warmup and two scored calls per photo,
serially. The 12 calls include 10 calls with owner item lists and two
theme-only calls; mean recall is the mean of the 10 per-call item recalls.
Theme match and JSON validity cover all 12 calls.

The pair analysis unions matched owner items for aligned calls. The companion
calls came from the matching full protocol on 2026-08-06; no new cloud calls
were made for this analysis. Modeled parallel latency is the per-call maximum
of the two stored wall times. It does not include orchestration overhead and
was not measured with both models serving concurrently.

The historical companion files and this Muse file predate per-call photo hashes
in `results.json`. They were aligned by the complete call identity set and
identical owner-evidence partitions, and the Opus 5 plus local 8B control
reproduced the previously recorded 0.832 union recall. New results include
photo hashes in the private raw file, and the scorer rejects photo mismatches.

## Muse candidate provenance

| Dimension | Value |
|---|---|
| Hugging Face repository | `meta-models/Muse-Glimmer-30B-GGUF` |
| Repository revision | `93769bc7ab5ad1e9cd22d857e3138cf5d977ae81` |
| Target artifact | `muse-glimmer-30B-kquant-17gb.gguf`, 16,756,681,056 bytes, SHA-256 `7e9b74b7c8875e9e265695df9613bf6290f2392e479ce740495a129019c488d8` |
| Vision projector | `mmproj-kquant.gguf`, SHA-256 `f48b452316f9b213758e8659444029b961a24a07f99a1abb2a9f88b06f7c00c6` |
| DFlash draft model | `dflash-kquant.gguf`, SHA-256 `27d9a805fa29b943cfb6ad4843367cd4eaaaf06bd452d8cc3e00a2cd18a677bc` |
| Runtime | llama.cpp `62bf73d25c53b8161f8a22894d4f90c4aebbd7d0`, CUDA 12.3.1, SM 86 |
| Inference settings | 16,384 context; full target/draft GPU offload requested; Flash Attention; Q8 K/V caches; one slot; batch/ubatch 512; reasoning off; DFlash maximum 15 draft tokens |
| Hardware | peecee RTX 3090 Ti, driver 596.49; 20,636 MiB observed GPU memory use |
| Draft behavior | 25.0% weighted acceptance across the smoke, warmup, and scored run activity |

The hosted companion model IDs are provider aliases observed on 2026-08-06;
provider-side immutable revisions were not exposed or captured. The local Qwen
tags also lack run-time artifact digests in the historical raw result. Those
gaps limit byte-exact reproduction and are reasons to append a new record when
rerunning the series.

## Solo result

| Model | Mean recall | Theme match | JSON valid | Errors | Latency s, min/mean/max |
|---|---:|---:|---:|---:|---:|
| Muse Glimmer 30B 17G | 0.639 | 1.000 | 1.000 | 0 | 10.31 / 12.17 / 14.37 |

## Two-model union results

| Pair | Mean union recall | Uplift over better member | Modeled parallel latency s, min/mean/max | Member error calls | Member invalid-JSON calls |
|---|---:|---:|---:|---:|---:|
| Muse + local Qwen3-VL 8B | **0.764** | **+0.125** | 10.50 / 20.82 / 59.07 | 0 | 1 |
| Muse + hosted Qwen3-VL 32B Instruct | 0.717 | +0.020 | 10.31 / 12.17 / 14.37 | 0 | 0 |
| Muse + Claude Opus 5 | 0.707 | +0.000 | 10.50 / 14.63 / 32.31 | 0 | 0 |
| Muse + local Qwen3-VL 32B | 0.703 | +0.064 | 17.93 / 58.83 / 96.12 | 0 | 0 |
| Muse + Gemini 3.6 Flash | 0.653 | +0.014 | 10.31 / 31.29 / 240.36 | 1 | 1 |
| Claude Opus 5 + local Qwen3-VL 8B control | **0.832** | **+0.125** | 8.14 / 21.66 / 59.07 | 0 | 1 |

The parallel-latency column is attainable only for a local-plus-cloud pair on
the current topology. Muse used 20,636 MiB and left 3,674 MiB free; the local
8B and 32B companions do not fit beside it on peecee's single GPU. Muse plus
local 8B is therefore a quality union, not a concurrently runnable ensemble.
The idealized sum of stored call times is 18.40 / 32.50 / 73.23 seconds
min/mean/max, before model unload and reload time. Muse plus local 32B is
similarly sequential at 28.43 / 71.00 / 106.43 seconds before swap overhead.

Muse and local Qwen3-VL 8B have complementary errors: across aligned calls,
Muse contributed eight match occurrences absent from the 8B result, the 8B
contributed five absent from Muse, and 18 were shared. Muse contributed no
match occurrence absent from Opus 5, so adding Muse to Opus produced no recall
uplift on this corpus.

The best Muse pair is therefore Muse plus local Qwen3-VL 8B at 0.764 mean
union recall. It trails the ADR 0006 Opus 5 plus local 8B control by 0.069.
Among Muse pairs that can run concurrently on the current topology, hosted
Qwen3-VL 32B is best at 0.717, only 0.020 above that companion alone. The Opus
5 plus local 8B control is higher-recall and parallel-feasible, but its ADR
0006 true-up remains planned rather than deployed. This small owner corpus
supports a relative comparison only; it does not prove general accuracy or
supply a promotion threshold.

## Decision boundary

The doctrine packet for this work was `pkt-cae60808f528ea35`, from corpus
`corpus-2026-07-12-a11702cc9217` and doctrine
`doctrine-f6bbb5196a3f8bf9`, with an `execute` authority ceiling. The result
uses the packet's benchmark-validity, baseline, metric-semantics,
repository-contract, and authority-boundary guidance. The owner has not set a
promotion target or quality/latency tradeoff for Muse, so this record makes no
promotion or deployment recommendation.

## Private evidence custody

The evidence files stay under `~/binkeeper-bench/` and are not committed.
Their hashes bind this sanitized record to the private results:

| Evidence | SHA-256 |
|---|---|
| Muse `results.json` | `38c4c841073f0e3d90a97def251703e444ac179b849f706e9239951ee4780b32` |
| Muse stage manifest | `1f0dabb84bdfa856e11791c758ea9ac1cbde3f3f62e80337531ff67a697a4a9e` |
| Muse runtime manifest | `f2f20454ff8ab5b4b3308a350ae1e7b39b4728fa9f716ae1f583600146b2a1c7` |
| Opus 5 results | `22bcdbd748ced8b374fc7a4aa56fa32dcd633808010fcaee821fbc1c81bbaa36` |
| Local Qwen results | `2c2a8ea0da5d9d9c91881b6bdff1cf9805ada9f528f76dcbdc6240ed9e4be3b0` |
| Hosted Qwen results | `86affa15e7327ed3d204b96c9b2803cdbc665f877c0a34a4413b38c135890c3d` |
| Gemini 3.6 Flash results | `62defb3f3956a380b9e62f82cb7cdfb1569927f6ffab7ea151c172b0aee785de` |

Future runs append a new record with a new record ID. A correction supersedes
this record by link and reason; it does not silently rewrite the measurement.
