"""Local content-addressed ciphertext storage owned by BinKeeper."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ENCRYPTION_ALGORITHM = "AES-256-GCM"
DEFAULT_BLOB_TENANT_ID = os.environ.get("BINKEEPER_BLOB_TENANT_ID", "personal")
DEFAULT_BLOB_CORPUS_ID = os.environ.get("BINKEEPER_BLOB_CORPUS_ID", "personal")
BLOB_VAULT_CONFIG_PATH = Path(
    os.environ.get(
        "BINKEEPER_BLOB_VAULT_CONFIG",
        str(Path.home() / ".config/binkeeper/blob-vault.json"),
    )
)


class BlobVaultError(ValueError):
    """A blob cannot be stored or verified safely."""


class BlobStore(Protocol):
    backend_name: str

    def put(self, object_key: str, data: bytes) -> None: ...

    def get(self, object_key: str) -> bytes: ...

    def exists(self, object_key: str) -> bool: ...


class InMemoryBlobStore:
    """Ciphertext-only ephemeral store for deterministic tests."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    def put(self, object_key: str, data: bytes) -> None:
        self._objects[object_key] = bytes(data)

    def get(self, object_key: str) -> bytes:
        try:
            return self._objects[object_key]
        except KeyError as exc:
            raise BlobVaultError(f"object {object_key!r} not found") from exc

    def exists(self, object_key: str) -> bool:
        return object_key in self._objects


class FilesystemBlobStore:
    """Ciphertext-only filesystem store under one local root."""

    backend_name = "filesystem"

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    def _path(self, object_key: str) -> Path:
        digest = object_key.rsplit("/", 1)[-1]
        if object_key != content_object_key(digest):
            raise BlobVaultError(f"refusing non-content-addressed key {object_key!r}")
        return self._root / object_key

    def put(self, object_key: str, data: bytes) -> None:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".partial")
        temporary.write_bytes(data)
        os.replace(temporary, path)

    def get(self, object_key: str) -> bytes:
        try:
            return self._path(object_key).read_bytes()
        except FileNotFoundError as exc:
            raise BlobVaultError(f"object {object_key!r} not found") from exc

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).exists()


@dataclass(frozen=True)
class BlobRecord:
    plaintext_sha256: str
    object_key: str
    byte_size: int
    already_existed: bool


def content_object_key(plaintext_sha256: str) -> str:
    digest = plaintext_sha256.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise BlobVaultError(f"invalid plaintext sha256 {plaintext_sha256!r}")
    return f"sha256/{digest[:2]}/{digest}"


def put_blob(
    conn: psycopg.Connection,
    store: BlobStore,
    data: bytes,
    *,
    key: bytes,
    key_ref: str,
    content_type: str | None = None,
    tenant_id: str = DEFAULT_BLOB_TENANT_ID,
    corpus_id: str = DEFAULT_BLOB_CORPUS_ID,
) -> BlobRecord:
    """Encrypt one blob and append its metadata idempotently."""
    _require_key(key)
    if not key_ref.strip():
        raise BlobVaultError("key_ref is required")
    digest = hashlib.sha256(data).hexdigest()
    object_key = content_object_key(digest)
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (digest,))
    existing = conn.execute(
        """
        SELECT byte_size FROM evidence_blobs
        WHERE tenant_id = %s AND corpus_id = %s AND plaintext_sha256 = %s
        """,
        (tenant_id, corpus_id, digest),
    ).fetchone()
    if existing is not None:
        return BlobRecord(digest, object_key, int(existing[0]), True)

    nonce = os.urandom(12)
    ciphertext = AESGCM(bytes(key)).encrypt(nonce, data, None)
    store.put(object_key, ciphertext)
    conn.execute(
        """
        INSERT INTO evidence_blobs (
            tenant_id, corpus_id, plaintext_sha256, byte_size, content_type, storage_backend,
            object_key, encryption_algorithm, ciphertext_sha256, nonce, key_ref,
            provenance
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '{}'::jsonb)
        """,
        (
            tenant_id,
            corpus_id,
            digest,
            len(data),
            content_type,
            store.backend_name,
            object_key,
            ENCRYPTION_ALGORITHM,
            hashlib.sha256(ciphertext).hexdigest(),
            nonce,
            key_ref.strip(),
        ),
    )
    return BlobRecord(digest, object_key, len(data), False)


def open_blob(
    conn: psycopg.Connection,
    store: BlobStore,
    plaintext_sha256: str,
    *,
    key: bytes,
    tenant_id: str = DEFAULT_BLOB_TENANT_ID,
    corpus_id: str = DEFAULT_BLOB_CORPUS_ID,
) -> bytes:
    """Restore a blob only after authentication and both hash checks."""
    _require_key(key)
    digest = plaintext_sha256.strip().lower()
    row = conn.execute(
        """
        SELECT object_key, ciphertext_sha256, nonce
        FROM evidence_blobs
        WHERE tenant_id = %s AND corpus_id = %s AND plaintext_sha256 = %s
        """,
        (tenant_id, corpus_id, digest),
    ).fetchone()
    if row is None:
        raise BlobVaultError(f"no blob for plaintext sha256 {digest!r}")
    ciphertext = store.get(str(row[0]))
    if hashlib.sha256(ciphertext).hexdigest() != row[1]:
        raise BlobVaultError(f"ciphertext hash mismatch for {digest!r}")
    try:
        plaintext = AESGCM(bytes(key)).decrypt(bytes(row[2]), ciphertext, None)
    except InvalidTag as exc:
        raise BlobVaultError(f"authentication failed for {digest!r}") from exc
    if hashlib.sha256(plaintext).hexdigest() != digest:
        raise BlobVaultError(f"plaintext hash mismatch for {digest!r}")
    return plaintext


def _require_key(key: bytes) -> None:
    if not isinstance(key, bytes | bytearray) or len(key) != 32:
        raise BlobVaultError("encryption key must be 32 bytes")


def load_vault_config(path: Path | None = None) -> Mapping[str, object]:
    """Load the owner-local vault configuration; it is never a package resource."""
    resolved = path or BLOB_VAULT_CONFIG_PATH
    try:
        parsed = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BlobVaultError(f"blob-vault config not found at {resolved}") from exc
    except json.JSONDecodeError as exc:
        raise BlobVaultError("blob-vault config is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise BlobVaultError("blob-vault config must be a JSON object")
    return {str(key): value for key, value in parsed.items()}


def vault_key_from_config(config: Mapping[str, object] | None = None) -> tuple[bytes, str]:
    """Read the local AES key and its audit reference."""
    selected = config if config is not None else load_vault_config()
    encoded = _required_text(selected.get("vault_key_b64"), "vault_key_b64")
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise BlobVaultError("vault_key_b64 is not valid base64") from exc
    _require_key(key)
    return key, _required_text(selected.get("key_ref"), "key_ref")


def blob_store_from_config(config: Mapping[str, object] | None = None) -> BlobStore:
    """Build the explicitly configured on-box ciphertext store."""
    selected = config if config is not None else load_vault_config()
    backend = selected.get("backend", "filesystem")
    if backend != "filesystem":
        raise BlobVaultError(f"unsupported local blob-vault backend {backend!r}")
    return FilesystemBlobStore(_required_text(selected.get("filesystem_root"), "filesystem_root"))


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlobVaultError(f"{field_name} is required")
    return value.strip()
