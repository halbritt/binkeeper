from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql

from binkeeper.blob_vault import FilesystemBlobStore, open_blob, put_blob
from binkeeper.migrations import migrate

DATABASE_URL = os.environ.get("BINKEEPER_TEST_DATABASE_URL")
EVIDENCE_TABLES = (
    "capture_evidence",
    "bin_trip_events",
    "location_observations",
    "bin_presence_events",
    "bin_resting_order_events",
    "bin_routing_requests",
    "bin_placement_decisions",
    "bin_item_liveness",
    "evidence_blobs",
)


@pytest.fixture()
def conn() -> Generator[psycopg.Connection, None, None]:
    if DATABASE_URL is None:
        pytest.skip("BINKEEPER_TEST_DATABASE_URL is required")
    connection = psycopg.connect(DATABASE_URL)
    migrate(connection)
    yield connection
    connection.close()


@pytest.mark.migration
def test_fresh_migration_and_rerun_are_idempotent(conn: psycopg.Connection) -> None:
    assert migrate(conn) == []
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    }
    assert set(EVIDENCE_TABLES) <= tables


@pytest.mark.migration
@pytest.mark.parametrize("table", EVIDENCE_TABLES)
def test_serving_role_cannot_write_evidence(conn: psycopg.Connection, table: str) -> None:
    conn.execute("SET ROLE binkeeper_serving")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(table)))
    finally:
        conn.rollback()


@pytest.mark.migration
def test_evidence_update_and_delete_are_rejected(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        INSERT INTO capture_evidence
            (external_id, captured_at, payload, privacy_class, provenance)
        VALUES ('synthetic-capture-1', now(), '{}'::jsonb, 'private', '{}'::jsonb)
        """
    )
    conn.commit()
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("UPDATE capture_evidence SET external_id = 'changed'")
    conn.rollback()


@pytest.mark.migration
def test_every_evidence_table_has_append_only_trigger(conn: psycopg.Connection) -> None:
    guarded = {
        row[0]
        for row in conn.execute(
            """
            SELECT event_object_table
            FROM information_schema.triggers
            WHERE trigger_schema = 'public'
              AND action_statement LIKE '%binkeeper_prevent_evidence_mutation%'
            """
        ).fetchall()
    }
    assert guarded == set(EVIDENCE_TABLES)
    with pytest.raises(psycopg.errors.RaiseException):
        conn.execute("DELETE FROM capture_evidence")
    conn.rollback()


@pytest.mark.migration
def test_schema_has_no_engram_dependency(conn: psycopg.Connection) -> None:
    definitions = "\n".join(
        row[0]
        for row in conn.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE connamespace = 'public'::regnamespace
            """
        ).fetchall()
    )
    assert "engram" not in definitions.lower()


@pytest.mark.migration
def test_synthetic_blob_round_trip_and_hash_verification(
    conn: psycopg.Connection, tmp_path: Path
) -> None:
    key = b"0123456789abcdef0123456789abcdef"
    data = b"synthetic bin photo bytes"
    store = FilesystemBlobStore(tmp_path)
    record = put_blob(conn, store, data, key=key, key_ref="synthetic-binkeeper-key")
    conn.commit()

    assert (tmp_path / record.object_key).read_bytes() != data
    assert open_blob(conn, store, record.plaintext_sha256, key=key) == data

    path = tmp_path / record.object_key
    ciphertext = path.read_bytes()
    path.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
    with pytest.raises(ValueError, match="ciphertext hash mismatch"):
        open_blob(conn, store, record.plaintext_sha256, key=key)
