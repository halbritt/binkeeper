# BinKeeper system review — BINK-ARCH-20261108

**Recommendation:** keep the local modular monolith and repair the creation,
cloud-export and physical-effect boundaries. The evidence does not justify a new
service, broker, inventory store or durable interactive analysis queue.

Review date: 2026-11-08. Source: `ed8ae090aa021c9b61a0752eabbe8dc321ab8595`.
Lead: agent-1315. Plane review: **BINK-53**. This is a completed assessment and
execution plan; the six follow-up items remain open. No runtime changes were made
as part of this review.

## Findings that determine the next work

| Finding | Evidence and consequence | Work |
| --- | --- | --- |
| Failed image preparation can send original bytes to a cloud provider | Both cloud clients reproduced this at a fake HTTP boundary, including original synthetic EXIF. This violates the accepted export contract. | **BINK-54**, urgent: fail closed before transport |
| Photo creation confuses a distinct request with a retry | Real PostgreSQL probes reproduced lost single-photo intent, photos linked to an earlier bin, and a next-day replay that restores an old location and prints again. | **BINK-55**, high: stable creation identity, atomic code allocation and complete error recovery |
| Printing has inconsistent durable recovery records | Source shows three different intent paths and response-only outcomes. A crash test remains required. | **BINK-56**, after BINK-55: common durable intent and observed handoff outcomes |
| Nightly review is degraded and its configured route conflicts with current policy | Latest run failed before inference because no local 8B slot was routable. Its Opus/OpenRouter pin conflicts with the owner's subscription-routing rule. | **BINK-57**: permitted route, dependency checks and visible advisory freshness |
| Request-local limits do not bound aggregate vision work | Two concurrent 13-photo requests admitted six model calls; saved-photo routes have a 12-photo policy. Production overload was not demonstrated. | **BINK-58**, after BINK-54/55: shared admission and measured recovery |
| Vault power-loss durability remains unverified | Source separates file publication from metadata commit and has no explicit file/directory fsync. No observed owner-data loss is claimed. | **BINK-59**: isolated recovery experiment before choosing a repair |

BINK-54 and BINK-55 are the first repairs. BINK-57 can proceed independently.
Keep originals, canonical ledgers, location ordering and owner-supplied text intact.
Leave the broader topology alone unless a new operating requirement or measured
limit earns a different boundary. Preserve returned form context now; persistent
drafts and persistent analysis are separate, conditional product decisions.

## Decision and evidence

- [Charter and authority](charter.md), [intent, scenarios and assumptions](intent.md).
- [Current system and critical flows](current-system.md), including refreshed
  installed-package, HTTPS and scheduled-job observations.
- [Candidate comparison and selected direction](candidates.md), including boundary
  removal counterfactuals, dissent and reopening conditions.
- [Executed checks and their limits](verification.md), with exact private synthetic
  artifact hashes in [probe-manifest.json](probe-manifest.json).
- [Migration, reversal limits and ordered work](migration.md), with stable Plane
  identities in [work-items.json](work-items.json).
- [Independent first proposal](independent-first.md), frozen before source exposure;
  [team, subscription route and coordination record](coordination.md).
- [Doctrine provenance and unresolved evidence obligations](doctrine.md).

The two verification assignments used synthetic images, disposable state and fake
model/printer transports. They did not send owner photos to models or print paper.
The installed package matched all 101 reviewed package/resource files; fronted
health, readiness and saved-photo GETs returned 200. These observations establish
the stated cases and current correspondence, not an availability or latency SLO.

The review stops here because the next actions are determined. General co-change
analysis, production saturation tests and another architecture proposal would not
change those repairs. Printer crash behavior and vault durability remain explicit
follow-up checks, not assumed properties.
