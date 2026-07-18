"""BinKeeper-owned PostgreSQL connection seam."""

from __future__ import annotations

import os
from typing import Literal

import psycopg


def connect(*, role: Literal["owner", "serving"] = "owner") -> psycopg.Connection:
    database_url = os.environ.get("BINKEEPER_DATABASE_URL")
    if not database_url:
        raise RuntimeError("BINKEEPER_DATABASE_URL is required")
    conn = psycopg.connect(database_url)
    if role == "serving":
        conn.execute("SET ROLE binkeeper_serving")
    return conn
