from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from binkeeper.blob_vault import BlobVaultError, FilesystemBlobStore, content_object_key


def test_content_object_key_is_validated() -> None:
    digest = hashlib.sha256(b"synthetic blob").hexdigest()
    assert content_object_key(digest) == f"sha256/{digest[:2]}/{digest}"
    with pytest.raises(BlobVaultError):
        content_object_key("not-a-digest")


def test_filesystem_store_rejects_traversal(tmp_path: Path) -> None:
    store = FilesystemBlobStore(tmp_path)
    with pytest.raises(BlobVaultError):
        store.get("../../owner-data")
