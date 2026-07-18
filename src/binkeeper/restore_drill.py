"""Bounded disposable restore drill for local scheduling."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from time import time_ns

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from binkeeper.backup import backup_key_from_config, check_backup_freshness, restore_smoke
from binkeeper.blob_vault import load_vault_config, vault_key_from_config


def build_parser() -> argparse.ArgumentParser:
    """Build the bounded restore-drill command surface."""
    parser = argparse.ArgumentParser(prog="binkeeper-restore-drill")
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
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=Path(os.environ.get("BINKEEPER_BACKUP_ROOT", "/var/lib/binkeeper/backups")),
    )
    parser.add_argument(
        "--max-age-seconds",
        type=int,
        default=int(os.environ.get("BINKEEPER_BACKUP_MAX_AGE_SECONDS", "90000")),
    )
    parser.add_argument(
        "--admin-database-url",
        default=os.environ.get("BINKEEPER_ADMIN_DATABASE_URL", "postgresql:///postgres"),
    )
    return parser


def main() -> None:
    """Create, verify, and remove one exact disposable restore target."""
    args = build_parser().parse_args()
    max_age = timedelta(seconds=args.max_age_seconds)
    config = load_vault_config(args.config)
    backup_key = backup_key_from_config(config)
    vault_key, _vault_key_ref = vault_key_from_config(config)
    freshness = check_backup_freshness(args.backup_root, backup_key=backup_key, max_age=max_age)
    target_name = f"binkeeper_restore_{time_ns()}_{os.getpid()}"
    target_url = make_conninfo(args.admin_database_url, dbname=target_name)
    created = False
    primary_error: BaseException | None = None
    try:
        with psycopg.connect(args.admin_database_url, autocommit=True) as admin:
            admin.execute(
                sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target_name))
            )
        created = True
        with tempfile.TemporaryDirectory(prefix="binkeeper-restored-blobs-") as blob_root:
            report = restore_smoke(
                artifact=freshness.artifact,
                target_database_url=target_url,
                restore_blob_root=Path(blob_root),
                backup_key=backup_key,
                vault_key=vault_key,
            )
        print(
            json.dumps(
                {
                    "status": "verified",
                    "artifact": str(freshness.artifact),
                    "table_counts": report.table_counts,
                    "blob_count": report.blob_count,
                    "passport_count": report.passport_count,
                },
                sort_keys=True,
            )
        )
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if created:
            try:
                with psycopg.connect(args.admin_database_url, autocommit=True) as admin:
                    admin.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        (target_name,),
                    )
                    admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_name)))
            except psycopg.Error as cleanup_error:
                if primary_error is not None:
                    raise RuntimeError(
                        f"restore drill failed and target cleanup also failed: {target_name}"
                    ) from cleanup_error
                raise RuntimeError(
                    f"restore drill target cleanup failed: {target_name}"
                ) from cleanup_error
