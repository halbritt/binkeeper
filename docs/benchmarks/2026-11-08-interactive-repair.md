# Interactive vision repair measurements — 2026-11-08

BINK-49, repository base `50470ac9925c03e0fa2e9435b9eaf75c381295e6`.
The source candidate uses `OpenRouterVisionClient` through the existing
`scripts/vision_bench.py` and `bin_vision_bench.run_model` seams. All candidates
use reasoning disabled, latency-sorted routing, the production prompt,
1280-pixel inference long edge, JPEG quality 85, and 3072 output tokens.

Six distinct photographed bins with owner-entered item lists were selected
in capture order, with two calls each per candidate. Calls were serial within
each model; no warmup was excluded. Raw item lists, photo identities, and
model outputs remain owner-local outside Git. Only the authorized inference
image and prompt were sent to OpenRouter; no writes or prints were performed.

| Model | Mean item recall | Theme match | Valid JSON | Errors | Seconds min / mean / max |
|---|---:|---:|---:|---:|---:|
| `qwen/qwen3.8-flash` | 0.71 | 0.92 | 12/12 | 0 | 3.5 / 4.6 / 6.2 |
| `qwen/qwen3.7-flash` | 0.67 | 0.83 | 12/12 | 0 | 2.6 / 3.9 / 6.4 |
| `qwen/qwen3.7-plus` | 0.66 | 0.75 | 12/12 | 0 | 4.0 / 6.8 / 10.1 |

Qwen3.8 Flash was selected for the stronger measured item coverage with a
small mean-latency difference from 3.7 Flash. Recall uses shared significant
tokens against full owner item lists; hidden items and unlisted visible items
limit the metric. This small comparison does not establish general accuracy,
false-positive rates, or tail latency. Provider load can change the ranking.

Private `results.json` SHA-256:
`a03bc1bf75fda9d49c46e19541db8d06fc5a6bab5ec9abf6fbc312327122d6e3`.

## Three-photo preparation

The first three stored benchmark photos were passed through the same selected
model and prompt. The baseline ran serial vision followed by serial QR
decoding. The repaired preparation ran three model requests concurrently;
QR decoding is deferred until after the response. Runs alternated serial,
parallel, parallel, serial. The measurement excludes upload, vault writes,
HTML rendering, registration, and physical printing.

| Preparation | Run 1 | Run 2 | Mean |
|---|---:|---:|---:|
| Serial vision plus blocking QR scans | 17.354 s | 13.552 s | 15.453 s |
| Parallel vision, QR scans after response | 5.962 s | 5.311 s | 5.637 s |

The measured mean preparation delay fell by 64%. This is a comparison on the
same supported model; the retired deployed model fails and has no successful
current latency baseline. The separate six-photo QR scan measured 0.9–1.8 s
per original. QR work is deferred, not eliminated. Tests separately verify
that the HTTP response is sent before that work starts and that concurrent
merging preserves upload order and partial-failure behavior.

OpenRouter API references: [reasoning controls](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
and [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
