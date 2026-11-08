# Saved-photo recovery verification — 2026-11-08

Work item: BINK-50. Repository: `halbritt/binkeeper`, branch `master`, base
`8576fa7a0b6b289de36e4677e8fd5da55f30de31`. No secondary worktree.

The owner reported that photos uploaded before the vision repair could not be
reopened after label creation failed. The originals were intact in the vault,
but there was no owner surface for selecting unattached uploads.

## Delivered behavior

`/saved-photos`, linked from New label and the proposal result, pages through
saved images with local thumbnails, timestamps and existing-bin reprint links.
The owner selects up to 12 photos of one bin and requests a new proposal. The
same reviewed confirmation form owns creation and printing. A model failure
still exposes manual entry; a vault outage retains selection and form details.

All original reads use the serving connection and configured tenant/corpus.
Gallery and thumbnails enforce the catalog's direct-loopback or paired-front
gate. Retry requires an explicit valid Origin. Preview JPEGs strip metadata and
use private/no-store headers. GETs do not invoke vision. Retry does not store
blobs or replay QR/location harvesting; an old image is not a fresh observation.

## Verification

- `BINKEEPER_TEST_DATABASE_URL=postgresql:///binkeeper_test_agent1315 make check`:
  lint, format and type checks passed; 583 tests passed; the separate migration
  gate passed 20 tests; wheel build and resource verification passed.
- Synthetic regression tests cover scoped reads, wrong-scope and non-image
  selection, authenticated-vault failure, pagination, order and deduplication,
  owner-front and Origin gates, metadata stripping, retained review after model
  failure, and unchanged evidence counts after retry.
- UIPass completed 27 cases across saved photos, photo drop and proposal
  results at 320, 390 and 1280 px widths. No blocking findings. Three console
  warnings correspond to the deliberate 503 outage fixture. Visual inspection
  led to a two-column phone gallery, aligned checkboxes, corrected numbering,
  a 12-image page bound and a jump to label details. Final inspected cases and
  limitations are recorded in
  `/tmp/binkeeper-1315-saved-photos-uipass-final/review.md`.
- The built wheel was installed into the existing production venv and the same
  `binkeeper.service` was restarted. Installed runtime files and templates
  match the checkout. No environment or schema change was required.
- Through `https://proximal.tail0ecc2e.ts.net:8766`: health and readiness passed;
  gallery HTML returned in 0.040 seconds; the latest three originals produced
  metadata-free thumbnails and a successful proposal in 6.804 seconds. This is
  one observed retry, not a latency guarantee. The review form retained the
  correct original-photo reference. Blob, capture, move and co-location counts
  remained unchanged. Registration and printing were not submitted.

Raw owner photos and proposal text were not copied into this repository.
Private deployment evidence is under
`~/.local/state/binkeeper/saved-photo-recovery-20261108/`.

## Authority and rollback

BinKeeper remains the sole runtime/data authority. Original evidence is
unchanged; the existing registration and print contracts remain in force.
ADR 0004's downscaled-image-and-prompt export boundary is unchanged. Rollback
restores the prior installed package from `previous-install.tar.gz` in the
private deployment directory and restarts the same service. No database or
model-pin rollback is needed.
