"""Deterministic read-only export and isolated BinKeeper import."""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, LiteralString, cast
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from binkeeper.migrations import migrate

SCHEMA_VERSION = "binkeeper-transfer/1"
TABLE_ORDER = (
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


class TransferMismatchError(RuntimeError):
    """The transfer contract or imported snapshot differs from its manifest."""


SOURCE_QUERIES = {
    "capture_evidence": """
        SELECT id, row_number() OVER (ORDER BY imported_at, external_id) AS seq,
               external_id, source_id::text AS source_external_id,
               COALESCE(observed_at, imported_at) AS captured_at,
               imported_at AS recorded_at, content_text, raw_payload AS payload,
               ('tier-' || privacy_tier::text) AS privacy_class,
               jsonb_build_object('source_table','captures','source_kind',source_kind::text,
                                  'tenant_id',tenant_id,'corpus_id',corpus_id) AS provenance
        FROM captures
        WHERE tenant_id = 'personal' AND corpus_id = 'personal'
          AND raw_payload->'metadata'->>'kind' = 'bin_capture'
        ORDER BY imported_at, external_id
    """,
    "bin_trip_events": """
        SELECT id, seq, external_id, event_kind, trip_id, bin_code, from_site, to_site, site,
               occurred_at, recorded_at, raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_trip_events','source_label',source_label,
                                  'tenant_id',tenant_id,'corpus_id',corpus_id) AS provenance
        FROM bin_trip_events WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "location_observations": """
        SELECT id, seq, external_id, bin_code, observed_site, source_kind,
               observation_strength, evidence_ref, observed_at, recorded_at,
               raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','location_observation','tenant_id',tenant_id,
                                  'corpus_id',corpus_id) AS provenance
        FROM location_observation WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "bin_presence_events": """
        SELECT id, seq, external_id, event_kind, site, occurred_at, recorded_at, accuracy_m,
               raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_presence_events','source_label',source_label,
                                  'tenant_id',tenant_id,'corpus_id',corpus_id) AS provenance
        FROM bin_presence_events WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "bin_resting_order_events": """
        SELECT id, seq, external_id, event_kind, order_id, site, bin_code, action_kind,
               instruction, cleared_reason, priority, occurred_at, recorded_at,
               raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_resting_order_events',
                                  'source_label',source_label,'tenant_id',tenant_id,
                                  'corpus_id',corpus_id) AS provenance
        FROM bin_resting_order_events
        WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "bin_routing_requests": """
        SELECT id, seq, external_id, input_text, site, requested_at, recorded_at, router_version,
               item_card_json AS item_card, route_result_json AS route_result, route_result_sha256,
               raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_routing_requests','request_kind',request_kind,
                                  'source_label',source_label,'tenant_id',tenant_id,
                                  'corpus_id',corpus_id) AS provenance
        FROM bin_routing_requests WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "bin_placement_decisions": """
        SELECT id, seq, external_id, routing_request_id, decision_kind, recommended_bin_code,
               selected_bin_code, actor, decided_at, recorded_at, reason,
               decision_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_placement_decisions',
                                  'raw_payload',raw_payload,'tenant_id',tenant_id,
                                  'corpus_id',corpus_id) AS provenance
        FROM bin_placement_decisions
        WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "bin_item_liveness": """
        SELECT id, seq, id::text AS external_id, phrase_norm, item_phrase, source_kind,
               source_ref, mentioned_at, harvested_at, harvester_version,
               raw_payload AS payload, 'private' AS privacy_class,
               jsonb_build_object('source_table','bin_item_liveness','tenant_id',tenant_id,
                                  'corpus_id',corpus_id) AS provenance
        FROM bin_item_liveness WHERE tenant_id='personal' AND corpus_id='personal' ORDER BY seq
    """,
    "evidence_blobs": """
        SELECT id, seq, plaintext_sha256, byte_size, content_type, storage_backend, object_key,
               encryption_algo AS encryption_algorithm, ciphertext_sha256, nonce, key_ref,
               created_at, 'private' AS privacy_class,
               jsonb_build_object('source_table','evidence_blobs','raw_payload',raw_payload,
                                  'tenant_id',tenant_id,'corpus_id',corpus_id) AS provenance
        FROM evidence_blobs
        WHERE tenant_id='personal' AND corpus_id='personal'
          AND plaintext_sha256 IN (
              SELECT raw_payload->'metadata'->'photo'->>'sha256' FROM captures
              WHERE tenant_id='personal' AND corpus_id='personal'
                AND raw_payload->'metadata'->>'kind'='bin_capture'
          )
        ORDER BY seq
    """,
}

JSON_COLUMNS = {"payload", "provenance", "item_card", "route_result"}
BYTE_COLUMNS = {"nonce"}
TARGET_SELECTS = {
    "capture_evidence": """SELECT id, seq, external_id, source_external_id, captured_at,
        recorded_at, content_text, payload, privacy_class, provenance
        FROM capture_evidence ORDER BY seq""",
    "bin_trip_events": """SELECT id, seq, external_id, event_kind, trip_id, bin_code,
        from_site, to_site, site, occurred_at, recorded_at, raw_payload AS payload,
        privacy_class, provenance FROM bin_trip_events ORDER BY seq""",
    "location_observations": """SELECT id, seq, external_id, bin_code, observed_site,
        source_kind, observation_strength, evidence_ref, observed_at, recorded_at, payload,
        privacy_class, provenance FROM location_observations ORDER BY seq""",
    "bin_presence_events": """SELECT id, seq, external_id, event_kind, site, occurred_at,
        recorded_at, accuracy_m, raw_payload AS payload, privacy_class, provenance
        FROM bin_presence_events ORDER BY seq""",
    "bin_resting_order_events": """SELECT id, seq, external_id, event_kind, order_id, site,
        bin_code, action_kind, instruction, cleared_reason, priority, occurred_at, recorded_at,
        raw_payload AS payload, privacy_class, provenance
        FROM bin_resting_order_events ORDER BY seq""",
    "bin_routing_requests": """SELECT id, seq, external_id, input_text, site, requested_at,
        recorded_at, router_version, item_card_json AS item_card,
        route_result_json AS route_result, route_result_sha256, raw_payload AS payload,
        privacy_class, provenance FROM bin_routing_requests ORDER BY seq""",
    "bin_placement_decisions": """SELECT id, seq, external_id, routing_request_id,
        decision_kind, recommended_bin_code, selected_bin_code, actor, decided_at, recorded_at,
        reason, decision_payload AS payload, privacy_class, provenance
        FROM bin_placement_decisions ORDER BY seq""",
    "bin_item_liveness": """SELECT id, seq, external_id, phrase_norm, item_phrase,
        source_kind, source_ref, mentioned_at, harvested_at, harvester_version,
        raw_payload AS payload, privacy_class, provenance FROM bin_item_liveness ORDER BY seq""",
    "evidence_blobs": """SELECT id, seq, plaintext_sha256, byte_size, content_type,
        storage_backend, object_key, encryption_algorithm, ciphertext_sha256, nonce, key_ref,
        created_at, privacy_class, provenance FROM evidence_blobs ORDER BY seq""",
}
PHYSICAL_COLUMNS: dict[str, dict[str, str]] = {
    "bin_trip_events": {"payload": "raw_payload"},
    "bin_presence_events": {"payload": "raw_payload"},
    "bin_resting_order_events": {"payload": "raw_payload"},
    "bin_routing_requests": {
        "item_card": "item_card_json",
        "route_result": "route_result_json",
        "payload": "raw_payload",
    },
    "bin_placement_decisions": {"payload": "decision_payload"},
    "bin_item_liveness": {"payload": "raw_payload"},
}
SOURCE_COLUMNS = {
    "captures": (
        "id",
        "source_id",
        "source_kind",
        "external_id",
        "imported_at",
        "raw_payload",
        "privacy_tier",
        "capture_type",
        "corrects_belief_id",
        "content_text",
        "observed_at",
        "tenant_id",
        "corpus_id",
        "bundle_id",
    ),
    "bin_trip_events": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "event_kind",
        "trip_id",
        "bin_code",
        "from_site",
        "to_site",
        "site",
        "occurred_at",
        "recorded_at",
        "source_label",
        "raw_payload",
    ),
    "location_observation": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "bin_code",
        "observed_site",
        "source_kind",
        "observation_strength",
        "evidence_ref",
        "observed_at",
        "recorded_at",
        "raw_payload",
    ),
    "bin_presence_events": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "event_kind",
        "site",
        "occurred_at",
        "recorded_at",
        "source_label",
        "accuracy_m",
        "raw_payload",
    ),
    "bin_resting_order_events": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "event_kind",
        "order_id",
        "site",
        "bin_code",
        "action_kind",
        "instruction",
        "cleared_reason",
        "priority",
        "occurred_at",
        "recorded_at",
        "source_label",
        "raw_payload",
    ),
    "bin_routing_requests": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "request_kind",
        "input_text",
        "site",
        "requested_at",
        "recorded_at",
        "source_label",
        "router_version",
        "item_card_json",
        "route_result_json",
        "route_result_sha256",
        "raw_payload",
    ),
    "bin_placement_decisions": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "external_id",
        "routing_request_id",
        "decision_kind",
        "recommended_bin_code",
        "selected_bin_code",
        "actor",
        "decided_at",
        "recorded_at",
        "reason",
        "decision_payload",
        "raw_payload",
    ),
    "bin_item_liveness": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "phrase_norm",
        "item_phrase",
        "source_kind",
        "source_ref",
        "mentioned_at",
        "harvested_at",
        "harvester_version",
        "raw_payload",
    ),
    "evidence_blobs": (
        "id",
        "seq",
        "tenant_id",
        "corpus_id",
        "plaintext_sha256",
        "byte_size",
        "content_type",
        "storage_backend",
        "object_key",
        "encryption_algo",
        "ciphertext_sha256",
        "nonce",
        "key_ref",
        "created_at",
        "raw_payload",
    ),
}


def export_engram(conn: psycopg.Connection) -> dict[str, Any]:
    """Read the pinned BinKeeper subset from Engram in a read-only transaction."""
    conn.execute("SET TRANSACTION READ ONLY")
    conn.execute("SET LOCAL statement_timeout = '30s'")
    _assert_source_schema(conn)
    tables: dict[str, list[dict[str, Any]]] = {}
    with conn.cursor(row_factory=dict_row) as cursor:
        for table in TABLE_ORDER:
            cursor.execute(cast(LiteralString, SOURCE_QUERIES[table]))
            tables[table] = [_normalize(dict(row)) for row in cursor.fetchall()]
    snapshot = {"schema_version": SCHEMA_VERSION, "tables": tables}
    snapshot["manifest"] = build_manifest(snapshot)
    return snapshot


def snapshot_binkeeper(conn: psycopg.Connection) -> dict[str, Any]:
    """Read canonical rows back from an isolated BinKeeper target."""
    tables: dict[str, list[dict[str, Any]]] = {}
    with conn.cursor(row_factory=dict_row) as cursor:
        for table in TABLE_ORDER:
            cursor.execute(cast(LiteralString, TARGET_SELECTS[table]))
            tables[table] = [
                _normalize({key: value for key, value in dict(row).items() if key != "seq"})
                | ({"seq": _normalize(row["seq"])} if "seq" in row else {})
                for row in cursor.fetchall()
            ]
    snapshot = {"schema_version": SCHEMA_VERSION, "tables": tables}
    snapshot["manifest"] = build_manifest(snapshot)
    return snapshot


def import_snapshot(conn: psycopg.Connection, snapshot: Mapping[str, Any]) -> None:
    """Import validated rows without any source-database capability."""
    verify_snapshot(snapshot)
    migrate(conn)
    tables = snapshot["tables"]
    assert isinstance(tables, Mapping)
    try:
        for table in TABLE_ORDER:
            rows = tables.get(table, [])
            if not isinstance(rows, Sequence):
                raise TransferMismatchError(f"tables.{table} is not a row sequence")
            for row in rows:
                _insert_row(conn, table, row)
        actual = snapshot_binkeeper(conn)
        if actual["manifest"] != snapshot["manifest"]:
            changed = sorted(
                key
                for key, value in actual["manifest"].items()
                if value != snapshot["manifest"].get(key)
            )
            raise TransferMismatchError(
                "imported target manifest differs from source manifest: " + ", ".join(changed)
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def build_manifest(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Hash every acceptance dimension independently and as one canonical whole."""
    tables = snapshot.get("tables")
    if not isinstance(tables, Mapping):
        raise TransferMismatchError("snapshot tables are missing")
    table_manifest: dict[str, Any] = {}
    for table in TABLE_ORDER:
        rows = tables.get(table, [])
        if not isinstance(rows, Sequence):
            raise TransferMismatchError(f"tables.{table} is not a row sequence")
        table_manifest[table] = {
            "count": len(rows),
            "ids_sha256": _hash([row.get("id") for row in rows]),
            "payloads_sha256": _hash(rows),
            "timestamps_sha256": _hash(_timestamp_values(rows)),
        }
    projections = _projections(tables)
    core = {
        "schema_version": SCHEMA_VERSION,
        "tables": table_manifest,
        "blob_hashes_sha256": _hash(
            [
                [row.get("plaintext_sha256"), row.get("ciphertext_sha256")]
                for row in tables.get("evidence_blobs", [])
            ]
        ),
        "location_folds_sha256": _hash(projections["locations"]),
        "trip_checksums_sha256": _hash(projections["trip_checksums"]),
        "passports_sha256": _hash(projections["passports"]),
        "route_receipts_sha256": _hash(projections["route_receipts"]),
    }
    return core | {"overall_sha256": _hash(core)}


def verify_snapshot(snapshot: Mapping[str, Any]) -> None:
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise TransferMismatchError("unsupported transfer schema")
    expected = snapshot.get("manifest")
    if not isinstance(expected, Mapping) or build_manifest(snapshot) != expected:
        raise TransferMismatchError("snapshot manifest mismatch")


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    verify_snapshot(snapshot)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(_canonical(snapshot) + "\n")


def read_snapshot(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise TransferMismatchError("snapshot root is not an object")
    verify_snapshot(parsed)
    return parsed


def _assert_source_schema(conn: psycopg.Connection) -> None:
    rows = conn.execute(
        """
        SELECT table_name, array_agg(column_name::text ORDER BY ordinal_position)
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = ANY(%s)
        GROUP BY table_name
        """,
        (list(SOURCE_COLUMNS),),
    ).fetchall()
    actual = {str(table): tuple(columns) for table, columns in rows}
    if actual != SOURCE_COLUMNS:
        raise TransferMismatchError(
            "Engram source schema differs from the pinned transfer contract"
        )


def _insert_row(conn: psycopg.Connection, table: str, raw_row: object) -> None:
    if not isinstance(raw_row, Mapping):
        raise TransferMismatchError(f"{table} row is not an object")
    row = dict(raw_row)
    columns = list(row)
    values = [
        Jsonb(row[name])
        if name in JSON_COLUMNS
        else base64.b64decode(row[name])
        if name in BYTE_COLUMNS
        else row[name]
        for name in columns
    ]
    statement = sql.SQL(
        "INSERT INTO {} ({}) OVERRIDING SYSTEM VALUE VALUES ({}) ON CONFLICT DO NOTHING"
    ).format(
        sql.Identifier(table),
        sql.SQL(", ").join(sql.Identifier(_physical_column(table, name)) for name in columns),
        sql.SQL(", ").join(sql.Placeholder() for _ in values),
    )
    conn.execute(statement, values)


def _physical_column(table: str, canonical: str) -> str:
    mapping = PHYSICAL_COLUMNS.get(table)
    return mapping[canonical] if mapping is not None and canonical in mapping else canonical


def _projections(tables: Mapping[str, Any]) -> dict[str, Any]:
    moves = list(tables.get("bin_trip_events", []))
    locations: dict[str, Any] = {}
    for row in moves:
        code = row.get("bin_code")
        if code and row.get("event_kind") in {"place", "load", "arrive"}:
            locations[str(code)] = {
                "state": "in_transit" if row["event_kind"] == "load" else "at_site",
                "site": None if row["event_kind"] == "load" else row.get("site"),
                "trip_id": row.get("trip_id") if row["event_kind"] == "load" else None,
                "seq": row.get("seq"),
            }
    trips: dict[str, dict[str, list[str]]] = {}
    for row in moves:
        trip = row.get("trip_id")
        code = row.get("bin_code")
        if trip and code and row.get("event_kind") in {"load", "arrive"}:
            trips.setdefault(str(trip), {"loaded": [], "arrived": []})[
                "loaded" if row["event_kind"] == "load" else "arrived"
            ].append(str(code))
    for value in trips.values():
        value["loaded"].sort()
        value["arrived"].sort()
    captures = tables.get("capture_evidence", [])
    passports: dict[str, list[str]] = {}
    for row in captures:
        payload = row.get("payload", {})
        metadata = payload.get("metadata", {}) if isinstance(payload, Mapping) else {}
        code = metadata.get("bin_code") if isinstance(metadata, Mapping) else None
        if code:
            passports.setdefault(str(code), []).append(_hash(row))
    routes = {
        str(row.get("external_id")): row.get("route_result_sha256")
        for row in tables.get("bin_routing_requests", [])
    }
    return {
        "locations": locations,
        "trip_checksums": trips,
        "passports": passports,
        "route_receipts": routes,
    }


def _timestamp_values(rows: Sequence[Any]) -> list[Any]:
    return [
        {key: value for key, value in row.items() if key.endswith("_at")}
        for row in rows
        if isinstance(row, Mapping)
    ]


def _normalize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, bytes | bytearray | memoryview):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID | Decimal):
        return str(value)
    return value


def _canonical(value: object) -> str:
    return json.dumps(_normalize(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def changed_snapshot(snapshot: Mapping[str, Any], dimension: str) -> dict[str, Any]:
    """Return a test-only deep copy with one protected dimension altered."""
    result = copy.deepcopy(dict(snapshot))
    manifest = result.get("manifest")
    if not isinstance(manifest, dict) or dimension not in manifest:
        raise KeyError(dimension)
    manifest[dimension] = "0" * 64
    return result


def main() -> None:
    """Export a read-only snapshot or import one into an isolated target."""
    parser = argparse.ArgumentParser(description="Transfer BinKeeper evidence with a hash manifest")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("source_url")
    export_parser.add_argument("output", type=Path)
    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("snapshot", type=Path)
    import_parser.add_argument("target_url")
    args = parser.parse_args()
    if args.command == "export":
        with psycopg.connect(args.source_url) as conn:
            snapshot = export_engram(conn)
            conn.rollback()
        write_snapshot(args.output, snapshot)
        print(snapshot["manifest"]["overall_sha256"])
    else:
        snapshot = read_snapshot(args.snapshot)
        with psycopg.connect(args.target_url) as conn:
            import_snapshot(conn, snapshot)
        print(snapshot["manifest"]["overall_sha256"])
