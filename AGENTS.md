# Project instructions

BinKeeper is a local-first physical inventory system. The repository is being
extracted from `~/git/engram`; read `README.md` and
`docs/extraction-analysis.md` before changing code or data contracts.

## Current status

The repository contains planning artifacts only. Engram remains the runtime and
data authority until a verified cutover work item explicitly changes that
status. Do not start a second writer or copy live owner data into this Git
repository.

## Architecture constraints

- Raw evidence is immutable and append-only.
- Derived state is rebuildable from canonical evidence.
- A bin's current location is a fold over move events, never an editable cell.
- Preserve provenance, confidence, idempotency keys, and audit history.
- Keep storage, models, printer access, and owner data local. Do not add hosted
  services, telemetry, cloud APIs, or external persistence without explicit
  owner approval.
- Tests and fixtures must be deterministic and synthetic. Never commit real bin
  contents, photos, coordinates, credentials, or database dumps.
- Tailnet-fronted HTTPS is the owner access path. Verify fronted behavior before
  accepting an owner-facing web change.

## Change discipline

- Keep structural extraction separate from behavior changes.
- Update the extraction analysis or a later accepted decision record when data
  ownership, compatibility, deployment, or cutover policy changes.
- Add or update tests for behavior changes.
- Use forward migrations. Do not rewrite or delete imported evidence.
- Commit each coherent verified slice and push `master`. Leave a clean tree and
  remove temporary worktrees or branches after merge.

## Tracking

- Plane workspace: `Proximal`
- Plane project: `BinKeeper` (`BINK`)
- GitHub repository: `https://github.com/halbritt/binkeeper`
- Use Plane, not GitHub Issues, for extraction work, reviews, and acceptance.
- Record the repository, branch or worktree, base SHA, verification evidence,
  data authority, and rollback scope in each implementation work item.
