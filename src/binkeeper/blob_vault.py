"""Local content-addressed ciphertext storage owned by BinKeeper."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

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
    storage_identity: str

    def put(self, object_key: str, data: bytes) -> None: ...

    def get(self, object_key: str) -> bytes: ...

    def exists(self, object_key: str) -> bool: ...


class InMemoryBlobStore:
    """Ciphertext-only ephemeral store for deterministic tests."""

    backend_name = "memory"

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self.storage_identity = f"memory:{id(self)}"

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
        self.storage_identity = f"filesystem:{self._root.expanduser().resolve()}"

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


class GarageObjectStore:
    """Ciphertext reader/writer for an explicitly local Garage S3 endpoint."""

    backend_name = "garage-s3"

    def __init__(
        self,
        *,
        endpoint: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        _require_local_endpoint(endpoint)
        self._endpoint = endpoint.rstrip("/")
        self._region = _required_text(region, "s3_region")
        self._bucket = _required_text(bucket, "bucket")
        self._access_key_id = _required_text(access_key_id, "access_key_id")
        self._secret_access_key = _required_text(secret_access_key, "secret_access_key")
        self._timeout_seconds = timeout_seconds
        self.storage_identity = f"garage-s3:{self._endpoint}/{self._bucket}"

    def _url(self, object_key: str) -> str:
        digest = object_key.rsplit("/", 1)[-1]
        if object_key != content_object_key(digest):
            raise BlobVaultError(f"refusing non-content-addressed key {object_key!r}")
        return f"{self._endpoint}/{self._bucket}/{object_key}"

    def _request(self, method: str, object_key: str, body: bytes = b"") -> bytes:
        request = urllib.request.Request(
            self._url(object_key), data=body if method == "PUT" else None, method=method
        )
        for name, value in self._signed_headers(method, object_key, body).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if method == "GET" and exc.code == 404:
                raise BlobVaultError(f"object {object_key!r} not found") from exc
            raise BlobVaultError(f"garage {method} refused object access: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise BlobVaultError(f"garage {method} endpoint is unreachable") from exc

    def _signed_headers(self, method: str, object_key: str, body: bytes) -> dict[str, str]:
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        host = urlparse(self._endpoint).netloc
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_uri = f"/{self._bucket}/{object_key}"
        canonical_headers = (
            f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = _sigv4_signing_key(self._secret_access_key, date_stamp, self._region)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        return {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def put(self, object_key: str, data: bytes) -> None:
        self._request("PUT", object_key, data)

    def get(self, object_key: str) -> bytes:
        return self._request("GET", object_key)

    def exists(self, object_key: str) -> bool:
        try:
            self._request("HEAD", object_key)
        except BlobVaultError:
            return False
        return True


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
    if backend == "filesystem":
        return FilesystemBlobStore(
            _required_text(selected.get("filesystem_root"), "filesystem_root")
        )
    if backend in {"garage", "garage-s3"}:
        return GarageObjectStore(
            endpoint=_required_text(selected.get("s3_endpoint"), "s3_endpoint"),
            region=_required_text(selected.get("s3_region"), "s3_region"),
            bucket=_required_text(selected.get("bucket"), "bucket"),
            access_key_id=_required_text(selected.get("access_key_id"), "access_key_id"),
            secret_access_key=_required_text(
                selected.get("secret_access_key"), "secret_access_key"
            ),
        )
    raise BlobVaultError(f"unsupported local blob-vault backend {backend!r}")


def _sigv4_signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    def sign(key: bytes, message: str) -> bytes:
        return hmac.new(key, message.encode(), hashlib.sha256).digest()

    date_key = sign(f"AWS4{secret}".encode(), date_stamp)
    region_key = sign(date_key, region)
    service_key = sign(region_key, "s3")
    return sign(service_key, "aws4_request")


def _require_local_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BlobVaultError("Garage endpoint must be an absolute local HTTP(S) URL")
    hostname = parsed.hostname
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname != "localhost" and not hostname.endswith(".ts.net"):
            raise BlobVaultError(
                "Garage endpoint must remain on loopback, private IP, or tailnet"
            ) from None
    else:
        if not (address.is_loopback or address.is_private):
            raise BlobVaultError("Garage endpoint must remain on loopback or a private IP")


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BlobVaultError(f"{field_name} is required")
    return value.strip()
