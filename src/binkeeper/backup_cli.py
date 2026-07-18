"""Operator CLI for local BinKeeper durability boundaries."""

from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta
from pathlib import Path

from binkeeper.backup import (
    BackupError,
    backup_key_from_config,
    check_backup_freshness,
    create_backup,
    restore_smoke,
)
from binkeeper.blob_vault import (
    BlobVaultError,
    blob_store_from_config,
    load_vault_config,
    vault_key_from_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="binkeeper-backup")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(
            os.environ.get(
                "BINKEEPER_BLOB_VAULT_CONFIG",
                str(Path.home() / ".config/binkeeper/blob-vault.json"),
            )
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--database-url", default=os.environ.get("BINKEEPER_DATABASE_URL"))
    create.add_argument(
        "--output-root",
        type=Path,
        default=Path(os.environ.get("BINKEEPER_BACKUP_ROOT", "/var/lib/binkeeper/backups")),
    )
    freshness = commands.add_parser("check-freshness")
    freshness.add_argument(
        "--backup-root",
        type=Path,
        default=Path(os.environ.get("BINKEEPER_BACKUP_ROOT", "/var/lib/binkeeper/backups")),
    )
    freshness.add_argument(
        "--max-age-seconds",
        type=int,
        default=int(os.environ.get("BINKEEPER_BACKUP_MAX_AGE_SECONDS", "90000")),
    )
    restore = commands.add_parser("restore-smoke")
    restore.add_argument("artifact", type=Path)
    restore.add_argument("--target-database-url", required=True)
    restore.add_argument("--restore-blob-root", type=Path, required=True)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = load_vault_config(args.config)
        backup_key = backup_key_from_config(config)
        if args.command == "create":
            if not args.database_url:
                parser.error("create requires --database-url or BINKEEPER_DATABASE_URL")
            artifact = create_backup(
                database_url=args.database_url,
                blob_store=blob_store_from_config(config),
                output_root=args.output_root,
                backup_key=backup_key,
            )
            print(json.dumps({"status": "created", "artifact": str(artifact)}, sort_keys=True))
            return
        if args.command == "check-freshness":
            result = check_backup_freshness(
                args.backup_root,
                backup_key=backup_key,
                max_age=timedelta(seconds=args.max_age_seconds),
            )
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "artifact": str(result.artifact),
                        "created_at": result.created_at.isoformat(),
                        "age_seconds": int(result.age.total_seconds()),
                    },
                    sort_keys=True,
                )
            )
            return
        vault_key, _key_ref = vault_key_from_config(config)
        report = restore_smoke(
            artifact=args.artifact,
            target_database_url=args.target_database_url,
            restore_blob_root=args.restore_blob_root,
            backup_key=backup_key,
            vault_key=vault_key,
        )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "table_counts": report.table_counts,
                    "blob_count": report.blob_count,
                    "passport_count": report.passport_count,
                },
                sort_keys=True,
            )
        )
    except (BackupError, BlobVaultError) as exc:
        parser.error(str(exc))
