# Backup and restore runbook

## Contract

Each artifact is one authenticated local directory containing a chunk-encrypted
PostgreSQL custom dump, the exact ciphertext bytes for every referenced blob,
and an HMAC-authenticated manifest. The manifest records migration checksums,
evidence counts and stable-row seals, roles, extensions, blob ciphertext and
plaintext hashes, non-secret key references, location/trip folds, passports,
and route-receipt hashes. It never contains a plaintext key.

The database dump uses independently authenticated 16 MiB AES-256-GCM chunks,
so restore does not depend on loading a growing database dump into memory. Blob
bytes are already encrypted by the vault and are copied byte-for-byte only
after their ciphertext hashes match.

## Provisioning

1. Create the production database from `template0`, then run
   `binkeeper-migrate`. This prevents unrelated extensions inherited from
   `template1` from entering the recovery contract.
2. Install the wheel under `/opt/binkeeper/venv` and create
   `/etc/binkeeper/blob-vault.json` from the example with mode 0600. The backup
   key must be different from the blob-vault key. Store another recoverable
   copy of both keys in the owner's offline key custody; Git and backup
   artifacts are not key custody.
3. Create `/var/lib/binkeeper/blobs` and `/var/lib/binkeeper/backups` with owner
   mode 0700. Set the backup path as `BINKEEPER_BACKUP_ROOT` in
   `/etc/binkeeper/binkeeper.env`; the blob path is the `filesystem_root`
   inside `/etc/binkeeper/blob-vault.json` (the environment file only points
   at that file).

Stop if the source database was not created from `template0`, the two key
references are missing, the keys are equal, the blob root contains a
non-content-addressed path, or the backup root is not owner-only.

## Create and check

Run one foreground backup before enabling a timer:

```sh
sudo -u halbritt /opt/binkeeper/venv/bin/binkeeper-backup create
sudo -u halbritt /opt/binkeeper/venv/bin/binkeeper-backup check-freshness
```

Creation exports one repeatable-read PostgreSQL snapshot, so the dump,
manifest, and referenced blob set share an authority boundary. A partial
directory is never accepted as an artifact. Command failure, a missing blob,
hash drift, manifest authentication failure, future timestamp, or age above
the configured maximum is a hard failure.

Only after the foreground commands pass may the operator install and enable
`binkeeper-backup.timer`. The timer retains artifacts; deletion or retention is
a separate owner policy and is not automated here. Inspect failures with:

```sh
systemctl status binkeeper-backup.service
journalctl -u binkeeper-backup.service --since today
```

## Disposable restore proof

The scheduled drill creates an exact random `binkeeper_restore_*` database from
`template0`, restores the newest fresh artifact, copies ciphertexts into a
temporary blob root, verifies every manifest dimension and decrypts every blob,
then terminates connections and drops only that exact database. It refuses any
other target prefix or non-empty target.

Run it once in the foreground before enabling its timer:

```sh
sudo -u halbritt /opt/binkeeper/venv/bin/binkeeper-restore-drill
```

Only then install and enable `binkeeper-restore-smoke.timer`. Inspect failures
with `systemctl status binkeeper-restore-smoke.service` and
`journalctl -u binkeeper-restore-smoke.service` (the unit is named
`restore-smoke`; the binary is `binkeeper-restore-drill`).

Stop and keep production authority unchanged after any migration, role,
extension, count, stable-row, payload, blob, fold, passport, trip, or route
mismatch. If cleanup fails, the command fails visibly and prints the exact
generated target name; the recovery owner must inspect it before dropping that
single database. Never use a wildcard or a broad database cleanup command.

## Recovery ownership

The `BINK-11` cutover completed on 2026-07-18; BinKeeper is the live authority
and its artifacts are the production recovery evidence. On backup failure the
executing operator owns the response: make `/readyz` unavailable, stop
BinKeeper writes (set `BINKEEPER_WRITES_ENABLED=0` in
`/etc/binkeeper/binkeeper.env` and restart `binkeeper.service`; the nightly
`binkeeper-ocr-harvest.timer` lane inherits the same gate from that file, or
stop it explicitly with `systemctl stop binkeeper-ocr-harvest.timer`),
preserve the newest artifacts and journals, and restore BinKeeper from a
verified artifact. The Engram-writer rollback window closed with the `BINK-13`
retirement (2026-07-18); the Engram writer must not be re-enabled, and any
further authority change requires a new owner-accepted work item. Never
rotate/destroy keys or delete Engram evidence during incident recovery.
