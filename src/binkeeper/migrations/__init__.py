"""Checksum-pinned forward migrations for the standalone database."""

from __future__ import annotations

import argparse
import hashlib
import os
from importlib import resources
from pathlib import Path
from typing import LiteralString, cast

import psycopg


class MigrationDriftError(RuntimeError):
    """An applied migration no longer matches its recorded checksum."""


def _migration_paths() -> tuple[Path, ...]:
    root = resources.files("binkeeper.migrations").joinpath("sql")
    return tuple(
        sorted(Path(str(entry)) for entry in root.iterdir() if entry.name.endswith(".sql"))
    )


def migration_names() -> tuple[str, ...]:
    """Return packaged SQL migrations in lexical order."""
    return tuple(path.name for path in _migration_paths())


def migrate(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations and reject changes to applied files."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()
    applied: list[str] = []
    for path in _migration_paths():
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        row = conn.execute(
            "SELECT checksum FROM schema_migrations WHERE filename = %s", (path.name,)
        ).fetchone()
        if row is not None:
            if row[0] != checksum:
                raise MigrationDriftError(f"applied migration changed: {path.name}")
            continue
        try:
            conn.execute(cast(LiteralString, path.read_text(encoding="utf-8")))
            conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        applied.append(path.name)
    return applied


def main() -> None:
    """Apply migrations to an explicit URL or BINKEEPER_DATABASE_URL."""
    parser = argparse.ArgumentParser(description="Apply BinKeeper database migrations")
    parser.add_argument("database_url", nargs="?", default=os.environ.get("BINKEEPER_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL required (argument or BINKEEPER_DATABASE_URL)")
    with psycopg.connect(args.database_url) as conn:
        for filename in migrate(conn):
            print(filename)
