# BINK-24 spike — router abstain quality on vision-derived item names

Date: 2026-07-19 · Base: `2dfacce` · Harness: `binkeeper bin-stash-route`
(read-only batch routing over the unchanged P1 router, `bin_stash.py`).

## Method

A 32-label corpus of vision-style item names in four buckets — clear matches
to the five registered bins' themes, vague vision output ("small black plastic
object"), foreign items the corpus does not own (should abstain), and
trait-heavy multiword labels ("black USB-C to USB-A cable 1m") — routed at
`alameda-garage` under three configurations:

1. **Real passports** (production, read-only): themes only; every `accepts`,
   `examples`, and `sibling_contents` is empty; capacity unknown.
2. **Enriched passports** (synthetic): the same five bins with plausible
   accepts/examples/siblings filled.
3. **Normalized labels** (trait tokens stripped) against the real theme-only
   passports.

## Results

| Configuration | Deck | Pending | Abstain rate | Wrong placements |
|---|---|---|---|---|
| 1. Real (theme-only) | 2/32 | 30/32 | 93.8% | 0 |
| 2. Enriched passports | 10/32 | 22/32 | 68.8% | 0 |
| 3. Normalized labels, theme-only | 0/32 | 32/32 | 100% | 0 |

- **The abstain gate is correctly conservative.** Zero false placements in 96
  routed decisions; every foreign and vague label abstained in every
  configuration (`no_accepting_passport` + `top_score_below_floor`). The
  feared rubber-stamp failure mode did not materialize at floor 0.60.
- **Theme-only passports starve the router.** With empty accepts/examples the
  only text signal is theme token overlap; items score a structural baseline
  (~0.41) plus whatever theme tokens they hit, and only near-verbatim theme
  matches clear the floor ("intel NUC mini pc" → AGR-001 at 0.600).
  Top-candidate choice was nonetheless correct for all ten clear-match items
  even while abstaining.
- **Passport enrichment is the dominant lever.** Filling accepts/examples/
  siblings took every clear-match item over the floor with the correct bin and
  left exactly the right items pending. Enrichment is what the swipe-deck
  decision loop (BINK-26) produces as a byproduct.
- **Label normalization is not worth building.** Stripping trait tokens gave
  no gain (config 3's zero deck is a borderline-floor artifact, not a
  penalty); the overlap dilution predicted for long labels is real but small
  next to the missing-vocabulary problem.
- **Cross-site matches are invisible by design.** EV-charging items routed at
  the garage cannot reach AST-001 (hard `wrong_site` filter). A later wave
  surface could show "belongs elsewhere" hints from the filtered candidates.

## Recommendation for BINK-25..28

Proceed without a vision-text normalization pass. Keep floor 0.60. Prioritize
the feedback loop that enriches passports from swipe decisions, since
vocabulary — not scoring — is the bottleneck. Quorum-birth clustering
(BINK-28) must cluster on item-to-item token overlap, never on router scores:
vague labels share a near-constant structural score and would false-cluster.
