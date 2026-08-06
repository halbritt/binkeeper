# Standalone deployment

The frozen standalone endpoint is loopback `127.0.0.1:8766`, already fronted
by tailnet-only HTTPS at `https://proximal.tail0ecc2e.ts.net:8766`. Engram keeps
its unrelated operator service on `:8765`; it no longer mounts or redirects
inventory paths. Do not change either mapping in a package install or service
restart.

The standalone catalog is mounted at `/bins/`; its photo, registration, and
management links target the standalone authoring mount at `/`, `/register`, and
`/manage/<bin-code>`. The retired Engram `/bin-photo/` prefix is not emitted by
the standalone catalog.

Install the wheel into `/opt/binkeeper/venv`, copy the reviewed unit from
`deploy/systemd/binkeeper.service`, and create `/etc/binkeeper/binkeeper.env`
from the example with owner-only permissions. Blob keys stay in the separate
owner-local vault configuration; they never belong in the environment file or
repository.

The environment file also selects the advisory vision backend. The interactive
default is the hosted OpenRouter backend (ADR 0005, model
`qwen/qwen3-vl-32b-instruct`), which exports only the downscaled inference
JPEG and prompt text under ADR 0004's scope; its API key lives in the
environment file. Rollback is one line plus a service restart:
`BINKEEPER_BIN_VISION_PROVIDER=gemini` (cloud fallback) or `local` (no
cloud). The service sandbox keeps the host filesystem read-only except for
the dedicated `/var/lib/binkeeper/blobs` vault root; backup artifacts remain
writable only by the separate backup unit.

Browser label printing remains unavailable until
`BINKEEPER_BIN_LABEL_CUPS_QUEUE` names an explicit local raw CUPS queue. During
reviewed registration, the owner may choose one or two labels; BinKeeper sends
that choice as one bounded TSPL job and never turns a replayed registration into
another printer attempt. The adjacent **Align label** button makes a separate,
strict-origin `POST /printer/align` request and disables itself while the one
feed job is pending. A timeout is reported as an unknown label position, so the
owner checks the stock before trying again; BinKeeper never retries the feed
automatically.

The button uses the configured label size. For the deployed 4-by-6-inch stock,
this equivalent TSPL command advances the raw
`OmezizyD450` queue to the beginning of the next label without printing:

```sh
printf 'SIZE 4,6\r\nFORMFEED\r\n' | lp -d OmezizyD450 -o raw
```

It physically consumes one feed step. `FORMFEED` is not sensor calibration; if
the printer repeatedly misses label boundaries, calibrate the loaded stock at
the printer before resuming BinKeeper jobs.

`BINKEEPER_WRITES_ENABLED` defaults to `0`. In that state every non-safe HTTP
method returns HTTP 503 before an authoring handler runs, the catalog hides
authoring links, and standalone CLI or MCP mutation commands fail before
appending evidence. Starting the process for a rehearsal therefore does not
start a second writer.
In production this gate has been open since the owner-approved `BINK-11`
one-writer cutover on 2026-07-18. Keep it `0` in every rehearsal or secondary
install; reopening it elsewhere, or moving writer authority again, requires a
new owner-accepted work item.

`/healthz` proves only that the process can answer. `/readyz` additionally
checks the read-only serving database path and requires an authenticated backup
no older than `BINKEEPER_BACKUP_MAX_AGE_SECONDS`. It returns HTTP 503 with an
explicit reason when either dependency is unavailable. A proxy must never turn
that result into a stale or cross-database read or write.

## Nightly peripheral-OCR true-up

`binkeeper-ocr-harvest.{service,timer}` runs `binkeeper bin-ocr-harvest
--local-only` nightly at 03:30. The lane OCRs every geolocated bin photo with
the local model and records corroborative `peripheral_ocr` observations; it is
idempotent, never relocates a bin, and exports nothing (ADR 0004/0005 are not
implicated). The unit layers `/etc/binkeeper/binkeeper-ocr-harvest.env` after
the service environment file: the pin selects the `local` provider, the peecee
endpoint and model, and the owner-local geofence file
(`~/.config/binkeeper/sites.json`, surveyed anchors, never in git). Fail-closed
properties: a missing pin file fails the unit start outright, and
`--local-only` aborts before any photo is read unless the effective provider
is `local` (a pin that lost only its model or endpoint lines still runs
locally on module defaults); exit 3 flags an unconfigured geofence; exit 4
flags a pass in which photos were read but no code was seen anywhere (the
usual sign of a dead or unpulled vision model). The pass runs autocommit so a
timeout or crash keeps the observations recorded so far. Known limitation: the
pass re-OCRs every eligible photo (dedupe is per observation, not per photo),
so wall-clock grows with the vault — revisit before the vault approaches the
unit's 4 h `TimeoutStartSec`. Rollback: `systemctl disable --now
binkeeper-ocr-harvest.timer`.

The reviewed backup and restore-smoke units are timer-driven templates, not an
authorization to install or enable them. Their key material belongs only in the
owner-local mode-0600 vault configuration. See
[runbooks/backup-restore.md](runbooks/backup-restore.md) before scheduling them.
Both timers are installed and enabled in production, activated after the
runbook's foreground backup and restore-drill gates passed ahead of the
`BINK-11` window; `/readyz` enforces the backup-freshness check they feed.

`BINK-11` enabled the service and completed the one-writer switch in its
owner-approved 2026-07-18 window. Changing authority again still requires an
explicit rollback or later accepted work item; deployment files and smoke tests
alone never authorize another writer transition.
