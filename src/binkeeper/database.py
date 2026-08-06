"""BinKeeper-owned PostgreSQL connection seam."""

from __future__ import annotations

import os
from typing import Literal

import psycopg


class ServingRoleUnavailableError(RuntimeError):
    """The process cannot assume BinKeeper's read-only serving role."""


def connect(
    *, role: Literal["owner", "serving"] = "owner", autocommit: bool = False
) -> psycopg.Connection:
    database_url = os.environ.get("BINKEEPER_DATABASE_URL")
    if not database_url:
        if role == "serving":
            raise ServingRoleUnavailableError("BINKEEPER_DATABASE_URL is required")
        raise RuntimeError("BINKEEPER_DATABASE_URL is required")
    conn = psycopg.connect(database_url, autocommit=autocommit)
    if role == "serving":
        try:
            conn.execute("SET ROLE binkeeper_serving")
            conn.commit()
        except psycopg.Error as exc:
            conn.close()
            raise ServingRoleUnavailableError(
                "cannot assume binkeeper_serving; run the standalone migrations"
            ) from exc
    return conn
