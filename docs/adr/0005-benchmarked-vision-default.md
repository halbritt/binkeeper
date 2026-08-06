# ADR 0005: Benchmark-selected OpenRouter default for the interactive vision pass

- Status: accepted
- Date: 2026-08-06

## Context

ADR 0004 authorized cloud vision and made Gemini (`gemini-3.5-flash`) the
default backend on the strength of a two-photo anecdote. The vision benchmark
built afterwards (`scripts/vision_bench.py`, six owner bins, two repeats,
owner item lists as ground truth) showed that anecdote was noise: the
deployed model scored worst of every live candidate (0.34 mean item recall,
0.67 JSON-contract rate) while hosted `qwen/qwen3-vl-32b-instruct` via
OpenRouter scored 0.70 recall — statistically tied with the best model
tested (claude-opus-5, 0.71) — at ~4.3 s mean latency and roughly 1/50th
opus's price. The owner's binding constraint is interactive latency on the
photo-drop page; heavyweight ensembles (13–21 s) are excluded from the first
pass regardless of quality.

## Decision

The advisory vision lane gains an `openrouter` provider (the existing
OpenAI-compatible client with a bearer token) and it becomes the default for
the interactive first pass, serving `qwen/qwen3-vl-32b-instruct`.

- `BINKEEPER_BIN_VISION_PROVIDER` now selects `openrouter` (default),
  `gemini`, or `local`. Gemini remains fully wired as the first fallback;
  the local path remains the no-cloud rollback.
- The OpenRouter key comes from `BINKEEPER_OPENROUTER_API_KEY` or
  `OPENROUTER_API_KEY`; a missing key raises `BinVisionError` inside the
  advisory lane, which degrades instead of failing the owner surface.
- The served model is configuration (`BINKEEPER_BIN_VISION_OPENROUTER_MODEL`),
  never code — Gemini retired two model names under this project in one day.

ADR 0004's export scope is unchanged and now applies to OpenRouter and its
upstream serving provider: only the downscaled inference JPEG and the lane's
prompt text leave the machine, from the vision lane only, and vision output
remains advisory everywhere.

## Consequences

Photo bytes now transit OpenRouter (and the provider it routes
`qwen/qwen3-vl-32b-instruct` to) rather than Google. The owner accepted this
provider change with the benchmark results in hand. Rollback is one
environment line: `BINKEEPER_BIN_VISION_PROVIDER=gemini` (cloud fallback) or
`local` (no cloud), plus a service restart.

A second, slower enrichment pass (ensemble or nightly local-32B true-up) is
deliberately out of scope here; it is planned separately and must land its
findings in an owner-review queue, never in the interactive path.

Sticking with `gemini-3.6-flash` (0.64 recall, ~5.2 s, would have been a
zero-code change) was rejected because the OpenRouter option is better on
recall, latency, and price simultaneously. Revisit if OpenRouter's routing
degrades the served quantization, per-photo cost or error rate rises
materially, or a rerun of the benchmark shows the ranking has moved.
