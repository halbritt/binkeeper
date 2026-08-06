# ADR 0004: Cloud vision backend for the advisory vision lane

- Status: accepted
- Date: 2026-08-06

## Context

The advisory vision lane (RFC 0088 T4 / RFC 0093 P5) has run exclusively
against a local Qwen3-VL 8B model on peecee. ADR-adjacent text in `README.md`,
`AGENTS.md`, and the `bin_vision` module docstring recorded a local-only
stance: no cloud calls, owner data stays on the local machine.

An owner-directed test on 2026-08-06 sent two vault photos (AGR-005, AST-002)
through the Gemini API (`gemini-3.5-flash`) using the lane's own prompt and
downscaling. Gemini returned roughly three times as many correctly identified
items on the cluttered bin, read brand text the local model misattributed,
and answered in 5–7 s against 16–18 s locally, at fractions of a cent per
photo. The owner then directed that the local-only vision decisions be
revised to use cloud calls.

## Decision

The advisory vision lane MAY export photo bytes to a cloud vision provider.
Gemini (`generativelanguage.googleapis.com`, default model
`gemini-3.5-flash`) becomes the default vision backend; the local Qwen3-VL
path remains available and selectable.

- `BINKEEPER_BIN_VISION_PROVIDER` selects `gemini` (default) or `local`.
- The API key comes from `BINKEEPER_GEMINI_API_KEY` or `GEMINI_API_KEY` in
  the service environment. A missing key raises `BinVisionError` inside the
  advisory lane, which already degrades instead of failing the owner surface.
- Only the downscaled inference JPEG (bounded long edge, same preparation as
  the local path) and the lane's prompt text are sent. Original photo bytes,
  bin codes, locations, and all other owner data stay local.
- The scope of the export is the vision lane only: label proposals, label-code
  OCR, and peripheral-code OCR. No other subsystem gains cloud access from
  this decision.

All prior vision invariants survive unchanged: vision output remains
advisory, cannot move or register a bin, cannot accept a placement, and every
proposal still requires an explicit owner action.

## Consequences

Owner photo content transits Google's API under Google's data-handling terms
whenever the provider is `gemini`. That is the deliberate trade for markedly
better item recall, brand OCR, and latency. Reverting is one environment
variable (`BINKEEPER_BIN_VISION_PROVIDER=local`) followed by a service
restart; no data or schema changes are involved.

The cloud call is an out-of-process integration point and is treated as
untrusted: bounded timeout, HTTP errors mapped to `BinVisionError`, and
response-shape validation before any text reaches the JSON parser. Tests
stay deterministic and synthetic; no live Gemini calls run in the suite.

Provider model churn is a real hazard: `gemini-2.5-flash` returned 404
("no longer available to new users") during the evaluation. The model name is
therefore configuration (`BINKEEPER_BIN_VISION_GEMINI_MODEL`), not code.

Supersede or revisit this decision if any of the following occurs: the
provider's data-handling terms change materially; per-photo cost or error
rate rises to where the local path's quality is competitive; a comparable
local model closes the recall/OCR gap; or the owner withdraws cloud
approval, in which case flipping the provider default back to `local` and
restoring the local-only invariant text is the complete rollback.

Keeping the local-only stance was rejected because the owner explicitly
weighed the photo-export privacy cost against measured quality and chose the
cloud path. Replacing the local path entirely was rejected because it is the
degradation target when the cloud endpoint or key is unavailable and the
rollback target for this decision.
