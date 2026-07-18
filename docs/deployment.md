# Standalone deployment

The frozen standalone endpoint is loopback `127.0.0.1:8766`, already fronted
by tailnet-only HTTPS at `https://proximal.tail0ecc2e.ts.net:8766`. Engram keeps
`:8765` throughout the compatibility window. Do not change either mapping in a
package install or service restart.

Install the wheel into `/opt/binkeeper/venv`, copy the reviewed unit from
`deploy/systemd/binkeeper.service`, and create `/etc/binkeeper/binkeeper.env`
from the example with owner-only permissions. Blob keys stay in the separate
owner-local vault configuration; they never belong in the environment file or
repository.

`/healthz` proves only that the process can answer. `/readyz` additionally
checks the read-only serving database path and requires an authenticated backup
no older than `BINKEEPER_BACKUP_MAX_AGE_SECONDS`. It returns HTTP 503 with an
explicit reason when either dependency is unavailable. A proxy or compatibility
shim must never turn that result into a stale direct Engram read or write.

The reviewed backup and restore-smoke units are timer-driven templates, not an
authorization to install or enable them. Their key material belongs only in the
owner-local mode-0600 vault configuration. See
[runbooks/backup-restore.md](runbooks/backup-restore.md) before scheduling them.

Deployment files and disposable local/fronted smoke tests do not authorize the
production writer. BINK-11 owns the service enablement, final route acceptance,
and one-writer switch after an owner-approved window.
