from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb

from binkeeper.backup import (
    EVIDENCE_RELATIONS,
    BackupError,
    BackupKey,
    backup_key_from_config,
    check_backup_freshness,
    create_backup,
    restore_smoke,
)
from binkeeper.bin_inventory import record_event
from binkeeper.blob_vault import FilesystemBlobStore, put_blob


def test_backup_protects_physical_containment_evidence() -> None:
    assert "bin_containment_events" in EVIDENCE_RELATIONS


def test_missing_backup_is_explicitly_unready(tmp_path: Path) -> None:
    with pytest.raises(BackupError, match="no verified backup"):
        check_backup_freshness(
            tmp_path,
            backup_key=BackupKey(b"b" * 32, "synthetic-backup-key"),
            max_age=timedelta(hours=25),
            now=datetime(2026, 7, 18, 12, tzinfo=UTC),
        )


def test_backup_key_must_be_distinct_from_blob_key() -> None:
    encoded = "dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnZ2dnY="
    with pytest.raises(BackupError, match="must differ"):
        backup_key_from_config(
            {
                "backup_key_b64": encoded,
                "backup_key_ref": "synthetic-backup-key",
                "vault_key_b64": encoded,
            }
        )


@pytest.mark.migration
def test_unmocked_backup_restore_verifies_all_protected_state(
    conn: psycopg.Connection,
    tmp_path: Path,
) -> None:
    database_url = os.environ.get("BINKEEPER_TEST_DATABASE_URL")
    assert database_url is not None
    as_of = datetime(2026, 7, 18, 12, tzinfo=UTC)
    backup_key = BackupKey(b"b" * 32, "synthetic-backup-key")
    vault_key = b"v" * 32
    source_blobs = FilesystemBlobStore(tmp_path / "source-blobs")

    conn.execute(
        """
        INSERT INTO capture_sources (source_kind, external_id, raw_payload)
        VALUES ('capture', 'synthetic-backup-source', '{"fixture": true}'::jsonb)
        """
    )
    conn.execute(
        """
        INSERT INTO capture_evidence (
            external_id, source_external_id, captured_at, recorded_at,
            content_text, payload, privacy_class, provenance
        ) VALUES (%s, %s, %s, %s, %s, %s, 'private', %s)
        """,
        (
            "synthetic-backup-capture",
            "synthetic-backup-source",
            as_of,
            as_of,
            "synthetic drill contents",
            Jsonb(
                {
                    "metadata": {
                        "kind": "bin_capture",
                        "bin_code": "TST-BACKUP",
                        "site": "site-a",
                        "bin_profile": {"theme": "Synthetic tools"},
                    }
                }
            ),
            Jsonb({"fixture": True}),
        ),
    )
    record_event(
        conn,
        event_kind="place",
        bin_code="TST-BACKUP",
        site="site-a",
        occurred_at=as_of,
        idempotency_key="synthetic-backup-place",
    )
    route_json = {"recommended_bin_code": "TST-BACKUP", "candidates": []}
    route_hash = hashlib.sha256(
        json.dumps(route_json, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    conn.execute(
        """
        INSERT INTO bin_routing_requests (
            external_id, input_text, site, requested_at, recorded_at,
            router_version, item_card_json, route_result_json,
            route_result_sha256, raw_payload
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
        """,
        (
            "synthetic-backup-route",
            "synthetic wrench",
            "site-a",
            as_of,
            as_of,
            "synthetic-router.v1",
            Jsonb({"text": "synthetic wrench"}),
            Jsonb(route_json),
            route_hash,
        ),
    )
    blob = put_blob(
        conn,
        source_blobs,
        b"synthetic encrypted photo",
        key=vault_key,
        key_ref="synthetic-vault-key",
        content_type="image/jpeg",
    )
    conn.commit()

    artifact = create_backup(
        database_url=database_url,
        blob_store=source_blobs,
        output_root=tmp_path / "backups",
        backup_key=backup_key,
        now=as_of,
    )
    manifest = json.loads((artifact / "manifest.json").read_text())
    assert "vault_key_b64" not in json.dumps(manifest)
    assert "backup_key" not in json.dumps(manifest).replace("backup_key_ref", "")
    assert b"synthetic drill contents" not in (artifact / "database.dump.enc").read_bytes()
    assert (artifact / "blobs" / blob.object_key).read_bytes() != b"synthetic encrypted photo"

    fresh = check_backup_freshness(
        tmp_path / "backups",
        backup_key=backup_key,
        max_age=timedelta(hours=25),
        now=as_of + timedelta(hours=24),
    )
    assert fresh.artifact == artifact
    with pytest.raises(BackupError, match="stale"):
        check_backup_freshness(
            tmp_path / "backups",
            backup_key=backup_key,
            max_age=timedelta(hours=25),
            now=as_of + timedelta(hours=26),
        )

    target_name = f"binkeeper_restore_test_{uuid4().hex[:12]}"
    target_url = f"postgresql:///{target_name}"
    with psycopg.connect("postgresql:///postgres", autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(target_name))
        )
    try:
        report = restore_smoke(
            artifact=artifact,
            target_database_url=target_url,
            restore_blob_root=tmp_path / "restored-blobs",
            backup_key=backup_key,
            vault_key=vault_key,
        )
        assert report.verified is True
        assert report.table_counts["capture_evidence"] == 1
        assert report.table_counts["bin_trip_events"] == 1
        assert report.table_counts["bin_routing_requests"] == 1
        assert report.blob_count == 1
        assert report.passport_count == 1
    finally:
        with psycopg.connect("postgresql:///postgres", autocommit=True) as admin:
            admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
                (target_name,),
            )
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(target_name)))

    manifest_path = artifact / "manifest.json"
    manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
    with pytest.raises(BackupError, match="verification failed"):
        check_backup_freshness(
            tmp_path / "backups",
            backup_key=backup_key,
            max_age=timedelta(hours=25),
            now=as_of + timedelta(hours=1),
        )
