from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

import pytest

from binkeeper.transfer import (
    SCHEMA_VERSION,
    TABLE_ORDER,
    TransferMismatchError,
    build_manifest,
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
