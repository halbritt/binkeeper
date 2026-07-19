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
repository. The service sandbox keeps the host filesystem read-only except for
the dedicated `/var/lib/binkeeper/blobs` vault root; backup artifacts remain
writable only by the separate backup unit.

Browser label printing remains unavailable until
`BINKEEPER_BIN_LABEL_CUPS_QUEUE` names an explicit local raw CUPS queue. During
reviewed registration, the owner may choose one or two labels; BinKeeper sends
that choice as one bounded TSPL job and never turns a replayed registration into
another printer attempt.

For the deployed 4-by-6-inch stock, this TSPL command advances the raw
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
Set it to `1` only at BINK-11's approved one-writer step after the Engram writer
has been frozen and its refusal recorded.

`/healthz` proves only that the process can answer. `/readyz` additionally
checks the read-only serving database path and requires an authenticated backup
no older than `BINKEEPER_BACKUP_MAX_AGE_SECONDS`. It returns HTTP 503 with an
explicit reason when either dependency is unavailable. A proxy must never turn
that result into a stale or cross-database read or write.

The reviewed backup and restore-smoke units are timer-driven templates, not an
authorization to install or enable them. Their key material belongs only in the
owner-local mode-0600 vault configuration. See
[runbooks/backup-restore.md](runbooks/backup-restore.md) before scheduling them.

`BINK-11` enabled the service and completed the one-writer switch in its
owner-approved 2026-07-18 window. Changing authority again still requires an
explicit rollback or later accepted work item; deployment files and smoke tests
alone never authorize another writer transition.
