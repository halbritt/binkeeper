from __future__ import annotations

import base64
import copy
import hashlib
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from binkeeper.blob_vault import InMemoryBlobStore, content_object_key
from binkeeper.transfer import (
    SCHEMA_VERSION,
    TABLE_ORDER,
    TransferMismatchError,
    build_manifest,
    stage_reencrypted_blobs,
    verify_blob_migration,
    verify_snapshot,
)


def empty_snapshot() -> dict[str, object]:
    snapshot: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "tables": {table: [] for table in TABLE_ORDER},
    }
    snapshot["manifest"] = build_manifest(snapshot)
    return snapshot


def test_empty_manifest_is_deterministic() -> None:
    first = empty_snapshot()
    assert build_manifest(first) == first["manifest"]
    verify_snapshot(first)


@pytest.mark.parametrize(
    "dimension",
    [
        "overall_sha256",
        "blob_hashes_sha256",
        "location_folds_sha256",
        "trip_checksums_sha256",
        "passports_sha256",
        "route_receipts_sha256",
    ],
)
def test_manifest_mismatch_fails_closed(dimension: str) -> None:
    snapshot = empty_snapshot()
    changed = copy.deepcopy(snapshot)
    changed["manifest"][dimension] = "0" * 64  # type: ignore[index]
    with pytest.raises(TransferMismatchError, match="manifest mismatch"):
        verify_snapshot(changed)


def test_count_id_and_payload_mismatches_fail_closed() -> None:
    snapshot = empty_snapshot()
    for field in ("count", "ids_sha256", "payloads_sha256"):
        changed = copy.deepcopy(snapshot)
        changed["manifest"]["tables"]["capture_evidence"][field] = "changed"  # type: ignore[index]
        with pytest.raises(TransferMismatchError, match="manifest mismatch"):
            verify_snapshot(changed)


def populated_snapshot() -> dict[str, object]:
    snapshot = empty_snapshot()
    tables = snapshot["tables"]  # type: ignore[assignment]
    tables["capture_evidence"] = [  # type: ignore[index]
        {"id": "capture-1", "payload": {"metadata": {"bin_code": "TST-001"}}}
    ]
    tables["bin_trip_events"] = [  # type: ignore[index]
        {
            "id": "move-1",
            "seq": 1,
            "event_kind": "load",
            "trip_id": "trip-1",
            "bin_code": "TST-001",
        },
        {
            "id": "move-2",
            "seq": 2,
            "event_kind": "arrive",
            "trip_id": "trip-1",
            "bin_code": "TST-001",
            "site": "site-b",
        },
    ]
    tables["evidence_blobs"] = [  # type: ignore[index]
        {"id": "blob-1", "plaintext_sha256": "1" * 64, "ciphertext_sha256": "2" * 64}
    ]
    tables["bin_routing_requests"] = [  # type: ignore[index]
        {"id": "route-1", "external_id": "route-1", "route_result_sha256": "3" * 64}
    ]
    snapshot["manifest"] = build_manifest(snapshot)
    return snapshot


@pytest.mark.parametrize(
    ("dimension", "mutate"),
    [
        ("count", lambda tables: tables["capture_evidence"].append({"id": "capture-2"})),
        ("id", lambda tables: tables["capture_evidence"][0].update(id="changed")),
        ("payload", lambda tables: tables["capture_evidence"][0]["payload"].update(extra=True)),
        ("blob", lambda tables: tables["evidence_blobs"][0].update(ciphertext_sha256="4" * 64)),
        ("fold", lambda tables: tables["bin_trip_events"][1].update(site="site-c")),
        ("checksum", lambda tables: tables["bin_trip_events"][1].update(bin_code="TST-002")),
        (
            "passport",
            lambda tables: tables["capture_evidence"][0]["payload"]["metadata"].update(
                bin_code="TST-002"
            ),
        ),
        (
            "route",
            lambda tables: tables["bin_routing_requests"][0].update(route_result_sha256="5" * 64),
        ),
    ],
)
def test_data_drift_fails_closed(dimension: str, mutate: Callable[[dict[str, Any]], None]) -> None:
    snapshot = populated_snapshot()
    mutate(snapshot["tables"])  # type: ignore[arg-type]
    with pytest.raises(TransferMismatchError, match="manifest mismatch"):
        verify_snapshot(snapshot)


def test_blob_staging_reencrypts_and_preserves_logical_evidence() -> None:
    source_key = b"s" * 32
    target_key = b"t" * 32
    plaintext = b"synthetic owner-free blob"
    digest = hashlib.sha256(plaintext).hexdigest()
    object_key = content_object_key(digest)
    source_nonce = b"n" * 12
    source_ciphertext = AESGCM(source_key).encrypt(source_nonce, plaintext, None)
    source_store = InMemoryBlobStore()
    source_store.put(object_key, source_ciphertext)
    target_store = InMemoryBlobStore()
    snapshot = empty_snapshot()
    tables = snapshot["tables"]
    assert isinstance(tables, dict)
    tables["evidence_blobs"] = [
        {
            "id": "blob-1",
            "seq": 1,
            "plaintext_sha256": digest,
            "byte_size": len(plaintext),
            "content_type": "image/jpeg",
            "storage_backend": "garage-s3",
            "object_key": object_key,
            "encryption_algorithm": "AES-256-GCM",
            "ciphertext_sha256": hashlib.sha256(source_ciphertext).hexdigest(),
            "nonce": base64.b64encode(source_nonce).decode("ascii"),
            "key_ref": "synthetic-source-key",
            "created_at": "2026-07-18T00:00:00+00:00",
            "privacy_class": "private",
            "provenance": {"fixture": True},
        }
    ]
    snapshot["manifest"] = build_manifest(snapshot)

    staged = stage_reencrypted_blobs(
        snapshot,
        source_store=source_store,
        source_key=source_key,
        target_store=target_store,
        target_key=target_key,
        target_key_ref="synthetic-target-key",
        nonce_source=lambda size: b"z" * size,
    )

    verify_blob_migration(snapshot, staged)
    staged_tables = staged["tables"]
    assert isinstance(staged_tables, dict)
    row = staged_tables["evidence_blobs"][0]
    assert row["id"] == "blob-1"
    assert row["plaintext_sha256"] == digest
    assert row["key_ref"] == "synthetic-target-key"
    target_ciphertext = target_store.get(object_key)
    assert target_ciphertext != source_ciphertext
    assert AESGCM(target_key).decrypt(b"z" * 12, target_ciphertext, None) == plaintext


def test_blob_staging_refuses_same_key_and_logical_drift() -> None:
    snapshot = empty_snapshot()
    store = InMemoryBlobStore()
    with pytest.raises(TransferMismatchError, match="must differ"):
        stage_reencrypted_blobs(
            snapshot,
            source_store=store,
            source_key=b"k" * 32,
            target_store=store,
            target_key=b"k" * 32,
            target_key_ref="synthetic-target-key",
        )
    with pytest.raises(TransferMismatchError, match="stores must be distinct"):
        stage_reencrypted_blobs(
            snapshot,
            source_store=store,
            source_key=b"s" * 32,
            target_store=store,
            target_key=b"t" * 32,
            target_key_ref="synthetic-target-key",
        )

    staged = copy.deepcopy(snapshot)
    manifest = snapshot["manifest"]
    assert isinstance(manifest, dict)
    staged["source_manifest"] = copy.deepcopy(manifest)
    staged["blob_migration"] = {
        "schema_version": "binkeeper-blob-migration/1",
        "blob_count": 0,
        "source_overall_sha256": manifest["overall_sha256"],
        "target_overall_sha256": manifest["overall_sha256"],
        "source_blob_hashes_sha256": manifest["blob_hashes_sha256"],
        "target_blob_hashes_sha256": manifest["blob_hashes_sha256"],
        "target_key_ref": "synthetic-target-key",
    }
    verify_blob_migration(snapshot, staged)
    staged_tables = staged["tables"]
    assert isinstance(staged_tables, dict)
    staged_tables["capture_evidence"] = [{"id": "drift"}]
    staged["manifest"] = build_manifest(staged)
    with pytest.raises(TransferMismatchError, match="protected table"):
        verify_blob_migration(snapshot, staged)
