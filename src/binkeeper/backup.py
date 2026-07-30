"""Authenticated local backup, restore-smoke, and freshness verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import sql

from binkeeper.bin_inventory import bin_where, trip_status
from binkeeper.bin_passport import bin_passport, load_known_bin_codes
from binkeeper.blob_vault import (
    BlobStore,
    FilesystemBlobStore,
    content_object_key,
    open_blob,
)

BACKUP_SCHEMA_VERSION: Final[str] = "binkeeper-backup/1"
DATABASE_ENCRYPTION: Final[str] = "AES-256-GCM/chunked-v1"
DATABASE_MAGIC: Final[bytes] = b"BINKDB1\n"
DATABASE_CHUNK_BYTES: Final[int] = 16 * 1024 * 1024
BACKUP_COMMAND_TIMEOUT_SECONDS: Final[int] = int(
    os.environ.get("BINKEEPER_BACKUP_COMMAND_TIMEOUT_SECONDS", "3600")
)
EVIDENCE_RELATIONS: Final[tuple[str, ...]] = (
    "capture_sources",
    "capture_evidence",
    "bin_trip_events",
    "bin_containment_events",
    "location_observations",
    "bin_presence_events",
    "bin_resting_order_events",
    "bin_routing_requests",
    "bin_placement_decisions",
    "bin_item_liveness",
    "evidence_blobs",
)
PROTECTED_RELATIONS: Final[tuple[str, ...]] = ("schema_migrations", *EVIDENCE_RELATIONS)


class BackupError(RuntimeError):
    """A backup cannot be created, trusted, or restored safely."""


@dataclass(frozen=True)
class BackupKey:
    """Owner-custodied backup key and non-secret audit reference."""

    key: bytes
    key_ref: str

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise BackupError("backup encryption key must be 32 bytes")
        if not self.key_ref.strip():
            raise BackupError("backup key reference is required")


@dataclass(frozen=True)
class BackupFreshness:
    """Verified newest artifact and its age."""

    artifact: Path
    created_at: datetime
    age: timedelta


@dataclass(frozen=True)
class RestoreReport:
    """Protected dimensions verified by a disposable restore."""

    verified: bool
    table_counts: dict[str, int]
    blob_count: int
    passport_count: int


def create_backup(
    *,
    database_url: str,
    blob_store: BlobStore,
    output_root: Path,
    backup_key: BackupKey,
    now: datetime | None = None,
) -> Path:
    """Create one atomic database-plus-ciphertext artifact from a shared snapshot."""
    created_at = _utc(now or datetime.now(UTC))
    output_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_root, 0o700)
    temporary = Path(tempfile.mkdtemp(prefix=".partial-", dir=output_root))
    try:
        plain_dump = temporary / "database.dump"
        encrypted_dump = temporary / "database.dump.enc"
        with psycopg.connect(database_url) as conn:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            snapshot_row = conn.execute("SELECT pg_export_snapshot()").fetchone()
            if snapshot_row is None:
                raise BackupError("database snapshot export returned no identifier")
            _run(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-acl",
                    f"--snapshot={snapshot_row[0]}",
                    f"--file={plain_dump}",
                    f"--dbname={database_url}",
                ],
                action="database dump",
            )
            state = _protected_state(conn, as_of=created_at)
            blob_rows = _blob_inventory(conn)
            database_name_row = conn.execute("SELECT current_database()").fetchone()
            if database_name_row is None:
                raise BackupError("database identity query returned no row")
            source_database = str(database_name_row[0])
            conn.rollback()

        dump_metadata = _encrypt_database_dump(plain_dump, encrypted_dump, backup_key.key)
        plain_dump.unlink()
        blob_manifest = _copy_blob_ciphertexts(blob_rows, blob_store, temporary / "blobs")
        manifest: dict[str, object] = {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": created_at.isoformat(),
            "source_database": source_database,
            "backup_key_ref": backup_key.key_ref.strip(),
            "database_encryption": DATABASE_ENCRYPTION,
            "database_chunk_bytes": DATABASE_CHUNK_BYTES,
            **dump_metadata,
            "state": state,
            "blobs": blob_manifest,
        }
        _write_signed_manifest(temporary, manifest, backup_key)
        suffix = str(dump_metadata["database_ciphertext_sha256"])[:12]
        final = output_root / f"binkeeper-{created_at.strftime('%Y%m%dT%H%M%SZ')}-{suffix}"
        if final.exists():
            raise BackupError(f"backup artifact already exists: {final.name}")
        os.replace(temporary, final)
        return final
    except BaseException:
        try:
            shutil.rmtree(temporary)
        except OSError as cleanup_error:
            raise BackupError(
                f"backup failed and partial artifact cleanup failed: {temporary.name}"
            ) from cleanup_error
        raise


def check_backup_freshness(
    backup_root: Path,
    *,
    backup_key: BackupKey,
    max_age: timedelta,
    now: datetime | None = None,
) -> BackupFreshness:
    """Require the newest local artifact to be authenticated and recent."""
    checked_at = _utc(now or datetime.now(UTC))
    if max_age <= timedelta(0):
        raise BackupError("maximum backup age must be positive")
    candidates = sorted(
        path
        for path in backup_root.glob("binkeeper-*")
        if path.is_dir() and not path.name.startswith(".partial-")
    )
    verified: list[tuple[datetime, Path]] = []
    for artifact in candidates:
        try:
            manifest = _load_verified_manifest(artifact, backup_key)
            created_at = _parse_time(manifest.get("created_at"), field="created_at")
        except BackupError as exc:
            raise BackupError(f"backup verification failed for {artifact.name}") from exc
        verified.append((created_at, artifact))
    if not verified:
        raise BackupError("no verified backup is available")
    created_at, artifact = max(verified, key=lambda row: row[0])
    age = checked_at - created_at
    if age < timedelta(minutes=-5):
        raise BackupError("newest backup timestamp is in the future")
    if age > max_age:
        raise BackupError(
            f"newest verified backup is stale: age {int(age.total_seconds())} seconds"
        )
    return BackupFreshness(artifact=artifact, created_at=created_at, age=max(age, timedelta(0)))


def restore_smoke(
    *,
    artifact: Path,
    target_database_url: str,
    restore_blob_root: Path,
    backup_key: BackupKey,
    vault_key: bytes,
) -> RestoreReport:
    """Restore into an empty disposable target and verify every protected dimension."""
    manifest = _load_verified_manifest(artifact, backup_key)
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupError("backup schema version is unsupported")
    expected_state = manifest.get("state")
    if not isinstance(expected_state, dict):
        raise BackupError("backup state manifest is invalid")
    as_of = _parse_time(manifest.get("created_at"), field="created_at")
    with psycopg.connect(target_database_url) as target:
        database_row = target.execute("SELECT current_database()").fetchone()
        if database_row is None or not str(database_row[0]).startswith("binkeeper_restore_"):
            raise BackupError("restore target must use the binkeeper_restore_ prefix")
        occupied = target.execute(
            """
            SELECT relname FROM pg_class
            WHERE relnamespace = 'public'::regnamespace
              AND relkind IN ('r', 'p', 'v', 'm', 'S')
            LIMIT 1
            """
        ).fetchone()
        if occupied is not None:
            raise BackupError("restore target is not empty")

    with tempfile.TemporaryDirectory(prefix="binkeeper-restore-") as scratch_text:
        plain_dump = Path(scratch_text) / "database.dump"
        _decrypt_database_dump(artifact / "database.dump.enc", plain_dump, manifest, backup_key.key)
        _run(
            [
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                f"--dbname={target_database_url}",
                str(plain_dump),
            ],
            action="database restore",
        )

    blob_entries = manifest.get("blobs")
    if not isinstance(blob_entries, list):
        raise BackupError("backup blob manifest is invalid")
    _restore_blob_ciphertexts(artifact, restore_blob_root, blob_entries)
    with psycopg.connect(target_database_url) as target:
        actual_state = _protected_state(target, as_of=as_of)
        if actual_state != expected_state:
            raise BackupError("restored protected state does not match the backup manifest")
        store = FilesystemBlobStore(restore_blob_root)
        for entry in blob_entries:
            if not isinstance(entry, dict):
                raise BackupError("backup blob entry is invalid")
            digest = entry.get("plaintext_sha256")
            if not isinstance(digest, str):
                raise BackupError("backup blob plaintext hash is invalid")
            open_blob(target, store, digest, key=vault_key)
        counts: dict[str, int] = {}
        for relation, details in _relation_details(actual_state).items():
            count = details.get("count")
            if not isinstance(count, int):
                raise BackupError("backup relation count is invalid")
            counts[relation] = count
        projections = actual_state.get("projections")
        passports = projections.get("passports") if isinstance(projections, dict) else None
        return RestoreReport(
            verified=True,
            table_counts=counts,
            blob_count=len(blob_entries),
            passport_count=len(passports) if isinstance(passports, dict) else 0,
        )


def _protected_state(conn: psycopg.Connection, *, as_of: datetime) -> dict[str, object]:
    relations: dict[str, object] = {}
    for relation in PROTECTED_RELATIONS:
        order_column = "filename" if relation == "schema_migrations" else "id"
        query = sql.SQL("SELECT to_jsonb(row_value)::text FROM {} AS row_value ORDER BY {}").format(
            sql.Identifier(relation), sql.Identifier(order_column)
        )
        rows = [str(row[0]) for row in conn.execute(query).fetchall()]
        relations[relation] = {
            "count": len(rows),
            "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest(),
        }

    roles = [
        {
            "name": str(row[0]),
            "can_login": bool(row[1]),
            "superuser": bool(row[2]),
            "create_role": bool(row[3]),
            "create_db": bool(row[4]),
        }
        for row in conn.execute(
            """
            SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb
            FROM pg_roles
            WHERE rolname IN ('binkeeper_owner', 'binkeeper_serving')
            ORDER BY rolname
            """
        ).fetchall()
    ]
    extensions = [
        {"name": str(row[0]), "version": str(row[1])}
        for row in conn.execute(
            "SELECT extname, extversion FROM pg_extension ORDER BY extname"
        ).fetchall()
    ]
    codes = load_known_bin_codes(conn)
    locations = {code: bin_where(conn, code).to_json() for code in codes}
    passports = {code: bin_passport(conn, code, now=as_of).to_json() for code in codes}
    trip_ids = [
        str(row[0])
        for row in conn.execute(
            """
            SELECT DISTINCT trip_id FROM bin_trip_events
            WHERE trip_id IS NOT NULL ORDER BY trip_id
            """
        ).fetchall()
    ]
    trips = {trip_id: trip_status(conn, trip_id).to_json() for trip_id in trip_ids}
    routes = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            """
            SELECT external_id, route_result_sha256
            FROM bin_routing_requests ORDER BY external_id
            """
        ).fetchall()
    }
    return {
        "relations": relations,
        "roles": roles,
        "extensions": extensions,
        "projections": {
            "as_of": as_of.isoformat(),
            "locations": locations,
            "passports": passports,
            "trips": trips,
            "route_receipts": routes,
        },
    }


def _blob_inventory(conn: psycopg.Connection) -> list[dict[str, object]]:
    return [
        {
            "plaintext_sha256": str(row[0]),
            "object_key": str(row[1]),
            "ciphertext_sha256": str(row[2]),
            "byte_size": int(row[3]),
            "key_ref": str(row[4]),
        }
        for row in conn.execute(
            """
            SELECT plaintext_sha256, object_key, ciphertext_sha256, byte_size, key_ref
            FROM evidence_blobs ORDER BY plaintext_sha256
            """
        ).fetchall()
    ]


def _copy_blob_ciphertexts(
    rows: list[dict[str, object]], store: BlobStore, destination: Path
) -> list[dict[str, object]]:
    copied: list[dict[str, object]] = []
    for row in rows:
        object_key = _validated_object_key(row)
        ciphertext = store.get(object_key)
        if hashlib.sha256(ciphertext).hexdigest() != row["ciphertext_sha256"]:
            raise BackupError(f"blob ciphertext hash mismatch for {object_key!r}")
        path = destination / object_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(ciphertext)
        os.chmod(path, 0o600)
        copied.append(dict(row))
    return copied


def _restore_blob_ciphertexts(artifact: Path, destination: Path, entries: list[object]) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(destination, 0o700)
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise BackupError("backup blob entry is invalid")
        object_key = _validated_object_key(raw_entry)
        expected_hash = raw_entry.get("ciphertext_sha256")
        if not isinstance(expected_hash, str):
            raise BackupError("backup blob entry is invalid")
        source = artifact / "blobs" / object_key
        try:
            ciphertext = source.read_bytes()
        except OSError as exc:
            raise BackupError(f"backup blob is missing for {object_key!r}") from exc
        if hashlib.sha256(ciphertext).hexdigest() != expected_hash:
            raise BackupError(f"backup blob ciphertext hash mismatch for {object_key!r}")
        target = destination / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(ciphertext)
        os.chmod(target, 0o600)


def _encrypt_database_dump(source: Path, destination: Path, key: bytes) -> dict[str, object]:
    aes = AESGCM(key)
    plaintext_hash = hashlib.sha256()
    with source.open("rb") as reader, destination.open("wb") as writer:
        writer.write(DATABASE_MAGIC)
        index = 0
        plaintext_bytes = 0
        while chunk := reader.read(DATABASE_CHUNK_BYTES):
            plaintext_hash.update(chunk)
            plaintext_bytes += len(chunk)
            nonce = os.urandom(12)
            ciphertext = aes.encrypt(nonce, chunk, index.to_bytes(8, "big"))
            writer.write(struct.pack(">Q", len(ciphertext)))
            writer.write(nonce)
            writer.write(ciphertext)
            index += 1
        writer.write(struct.pack(">Q", 0))
    os.chmod(destination, 0o600)
    return {
        "database_plaintext_sha256": plaintext_hash.hexdigest(),
        "database_ciphertext_sha256": _file_sha256(destination),
        "database_plaintext_bytes": plaintext_bytes,
        "database_ciphertext_bytes": destination.stat().st_size,
        "database_chunk_count": index,
    }


def _decrypt_database_dump(
    source: Path, destination: Path, manifest: dict[str, object], key: bytes
) -> None:
    expected_ciphertext = manifest.get("database_ciphertext_sha256")
    if not isinstance(expected_ciphertext, str) or _file_sha256(source) != expected_ciphertext:
        raise BackupError("database backup ciphertext hash mismatch")
    aes = AESGCM(key)
    plaintext_hash = hashlib.sha256()
    with source.open("rb") as reader, destination.open("wb") as writer:
        if reader.read(len(DATABASE_MAGIC)) != DATABASE_MAGIC:
            raise BackupError("database backup header is invalid")
        index = 0
        while True:
            length_bytes = reader.read(8)
            if len(length_bytes) != 8:
                raise BackupError("database backup ended before a chunk length")
            length = struct.unpack(">Q", length_bytes)[0]
            if length == 0:
                break
            nonce = reader.read(12)
            ciphertext = reader.read(length)
            if len(nonce) != 12 or len(ciphertext) != length:
                raise BackupError("database backup ended inside an encrypted chunk")
            try:
                plaintext = aes.decrypt(nonce, ciphertext, index.to_bytes(8, "big"))
            except InvalidTag as exc:
                raise BackupError("database backup authentication failed") from exc
            plaintext_hash.update(plaintext)
            writer.write(plaintext)
            index += 1
        if reader.read(1):
            raise BackupError("database backup has trailing bytes")
    expected_plaintext = manifest.get("database_plaintext_sha256")
    if not isinstance(expected_plaintext, str) or plaintext_hash.hexdigest() != expected_plaintext:
        raise BackupError("database backup plaintext hash mismatch")


def _write_signed_manifest(artifact: Path, manifest: dict[str, object], key: BackupKey) -> None:
    payload = _canonical_json(manifest)
    (artifact / "manifest.json").write_bytes(payload)
    signature = hmac.new(key.key, payload, hashlib.sha256).hexdigest()
    (artifact / "manifest.hmac-sha256").write_text(signature + "\n", encoding="ascii")
    os.chmod(artifact / "manifest.json", 0o600)
    os.chmod(artifact / "manifest.hmac-sha256", 0o600)


def _load_verified_manifest(artifact: Path, key: BackupKey) -> dict[str, object]:
    try:
        payload = (artifact / "manifest.json").read_bytes()
        signature = (artifact / "manifest.hmac-sha256").read_text(encoding="ascii").strip()
    except OSError as exc:
        raise BackupError("backup manifest is missing") from exc
    expected = hmac.new(key.key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise BackupError("backup manifest authentication failed")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BackupError("backup manifest is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise BackupError("backup manifest must be an object")
    return {str(name): value for name, value in parsed.items()}


def _relation_details(state: dict[str, object]) -> dict[str, dict[str, object]]:
    relations = state.get("relations")
    if not isinstance(relations, dict):
        raise BackupError("backup relation manifest is invalid")
    details: dict[str, dict[str, object]] = {}
    for name, value in relations.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            raise BackupError("backup relation entry is invalid")
        details[name] = value
    return details


def _run(argv: list[str], *, action: str) -> None:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            shell=False,
            timeout=BACKUP_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackupError(f"{action} command is unavailable") from exc
    if completed.returncode != 0:
        raise BackupError(f"{action} command was refused")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_object_key(entry: Mapping[str, object]) -> str:
    object_key = entry.get("object_key")
    plaintext_hash = entry.get("plaintext_sha256")
    if not isinstance(object_key, str) or not isinstance(plaintext_hash, str):
        raise BackupError("backup blob entry is invalid")
    try:
        expected_key = content_object_key(plaintext_hash)
    except ValueError as exc:
        raise BackupError("backup blob plaintext hash is invalid") from exc
    if object_key != expected_key:
        raise BackupError("backup blob object key is not content-addressed")
    return object_key


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _parse_time(value: object, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise BackupError(f"backup {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BackupError(f"backup {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise BackupError(f"backup {field} must include a timezone")
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise BackupError("backup time must include a timezone")
    return value.astimezone(UTC)


def backup_key_from_config(config: Mapping[str, object]) -> BackupKey:
    """Load a separate backup key without placing it in an artifact."""
    encoded = config.get("backup_key_b64")
    key_ref = config.get("backup_key_ref")
    if not isinstance(encoded, str) or not isinstance(key_ref, str):
        raise BackupError("backup key configuration is incomplete")
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise BackupError("backup_key_b64 is invalid") from exc
    vault_encoded = config.get("vault_key_b64")
    if isinstance(vault_encoded, str):
        try:
            vault_key = base64.b64decode(vault_encoded, validate=True)
        except ValueError as exc:
            raise BackupError("vault_key_b64 is invalid") from exc
        if hmac.compare_digest(key, vault_key):
            raise BackupError("backup encryption key must differ from the blob-vault key")
    return BackupKey(key=key, key_ref=key_ref)
