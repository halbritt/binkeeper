# Project instructions

BinKeeper is a local-first physical inventory system. The repository is being
extracted from `~/git/engram`; read `README.md` and
`docs/extraction-analysis.md` before changing code or data contracts.

## Current status

The owner-approved `BINK-11` cutover completed on 2026-07-18. BinKeeper is the
sole runtime and data authority for new physical-inventory evidence. Engram's
historical rows remain immutable provenance and its legacy names are temporary
compatibility redirects or subprocess calls into BinKeeper. Do not re-enable
the Engram writer, start a second writer, or copy live owner data into this Git
repository.

## Architecture constraints

- Raw evidence is immutable and append-only.
- Derived state is rebuildable from canonical evidence.
- A bin's current location is a fold over move events, never an editable cell.
- Preserve provenance, confidence, idempotency keys, and audit history.
- Keep storage, models, printer access, and owner data local. Do not add hosted
  services, telemetry, cloud APIs, or external persistence without explicit
  owner approval. The one accepted exception is ADR 0004: the advisory vision
  lane may call the configured cloud vision provider with the downscaled
  inference image and prompt text only.
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

## Parallel work: one worktree per branch

When more than one agent works this repo at once, do not share a working
directory — give each unit of work its own git worktree. A branch can be
checked out in only one worktree at a time, so concurrent edits to shared
files (Makefile, configs, generated/golden files) become impossible.

- One worktree per branch, one agent per worktree; name the dir after the branch.
- Siblings, not nested: create worktrees OUTSIDE this checkout
  (`../binkeeper-wt/<branch>`), never inside it — recursive globs, file-count/hash
  gates, and IDE indexers must not scan across worktrees.
- Lifecycle: `git worktree add ../binkeeper-wt/<branch> -b <branch>` /
  `git worktree list` / `git worktree remove <path>` after merge /
  `git worktree prune`. Agents with worktree isolation get this for free.
- Shared object store and build caches are fine; worktrees do NOT isolate
  ports, databases, or local services — coordinate those separately.
- Regenerate, don't merge, generated artifacts (golden files, compiled
  indexes): merge the source change, then regenerate once on the merged tree.
