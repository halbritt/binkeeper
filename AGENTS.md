# Project instructions

BinKeeper is a local-first physical inventory system. The repository was
extracted from `~/git/engram` (extraction complete; BinKeeper is the
standalone authority); read `README.md`, `docs/architecture.md`, and
`docs/extraction-analysis.md` before changing code or data contracts.

## Current status

The owner-approved `BINK-11` cutover completed on 2026-07-18. BinKeeper is the
sole runtime and data authority for new physical-inventory evidence. Engram's
historical rows remain immutable provenance; its compatibility names, shims,
and embedded BinKeeper runtime were retired under `BINK-13` on 2026-07-18, so
legacy Engram calls are explicitly unavailable, not redirected (see
`docs/runbooks/compatibility-retirement.md`). Do not re-enable the Engram
writer, start a second writer, or copy live owner data into this Git
repository.

Since 2026-08-06 the interactive advisory-vision default is the hosted
OpenRouter `qwen/qwen3-vl-32b-instruct` backend (ADR 0005), selected by
`BINKEEPER_BIN_VISION_PROVIDER` (`openrouter` default; `gemini` and `local`
remain the fallback and the no-cloud rollback). A nightly local-only
peripheral-OCR location true-up is deployed as
`binkeeper-ocr-harvest.{service,timer}` (`deploy/systemd/`, 03:30): it runs
`binkeeper bin-ocr-harvest --local-only` with the local peecee `qwen3-vl:32b`
pinned by an environment pin file, and fails closed (exit 3 = no geofence site
configured, exit 4 = photos read but zero codes seen). ADR 0006 (accepted
2026-08-06) is deployed as `binkeeper-label-drift.{service,timer}`
(`deploy/systemd/`, 04:00 with up to 15 minutes of jitter, ordered after OCR).
It runs an input-keyed union of OpenRouter `anthropic/claude-opus-5` and an
exact gpu-fleet lease for peecee `qwen3-vl:8b`, then writes append-only
proposals for the rebuildable owner review queue. A model error fails the
affected bin closed; exit 5 reports one or more model failures. `BINK-42`..
`BINK-44` completed on 2026-08-11.

## Architecture constraints

- Raw evidence is immutable and append-only.
- Derived state is rebuildable from canonical evidence.
- A bin's current location is a fold over move events, never an editable cell.
- Preserve provenance, confidence, idempotency keys, and audit history.
- Keep storage, models, printer access, and owner data local. Do not add hosted
  services, telemetry, cloud APIs, or external persistence without explicit
  owner approval. The one accepted export exception is ADR 0004: the advisory
  vision lane may call the configured cloud vision provider with the
  downscaled inference image and prompt text only. ADR 0005 selects the
  current default provider inside that unchanged scope; ADR 0006 adds the
  deployed nightly ensemble upstream under the same scope.
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
