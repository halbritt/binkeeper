from __future__ import annotations

import os
from collections.abc import Generator

import psycopg
import pytest

from binkeeper.migrations import migrate

EVIDENCE_TABLES = (
    "capture_sources",
    "capture_evidence",
    "bin_trip_events",
    "location_observations",
    "bin_presence_events",
    "bin_resting_order_events",
    "bin_routing_requests",
    "stash_runs",
    "colocation_observations",
    "bin_placement_decisions",
    "bin_item_liveness",
    "evidence_blobs",
)


@pytest.fixture()
def conn() -> Generator[psycopg.Connection, None, None]:
    database_url = os.environ.get("BINKEEPER_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("BINKEEPER_TEST_DATABASE_URL is required")
    connection = psycopg.connect(database_url, autocommit=True)
    migrate(connection)
    connection.execute("TRUNCATE " + ", ".join(EVIDENCE_TABLES) + " RESTART IDENTITY CASCADE")
    yield connection
    connection.close()
