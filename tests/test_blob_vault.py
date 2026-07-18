from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from binkeeper.blob_vault import (
    BlobVaultError,
    FilesystemBlobStore,
    GarageObjectStore,
    content_object_key,
)


def test_content_object_key_is_validated() -> None:
    digest = hashlib.sha256(b"synthetic blob").hexdigest()
    assert content_object_key(digest) == f"sha256/{digest[:2]}/{digest}"
    with pytest.raises(BlobVaultError):
        content_object_key("not-a-digest")


def test_filesystem_store_rejects_traversal(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    with pytest.raises(BlobVaultError):
        store.get("../../owner-data")


def test_garage_store_refuses_public_endpoints() -> None:
    with pytest.raises(BlobVaultError, match="loopback, private IP, or tailnet"):
        GarageObjectStore(
            endpoint="https://objects.example.com",
            region="local",
            bucket="synthetic",
            access_key_id="synthetic-access",
            secret_access_key="synthetic-secret",
        )


def test_garage_store_accepts_loopback_without_contacting_it() -> None:
    store = GarageObjectStore(
        endpoint="http://127.0.0.1:3900",
        region="local",
        bucket="synthetic",
        access_key_id="synthetic-access",
        secret_access_key="synthetic-secret",
    )
    assert store.backend_name == "garage-s3"
