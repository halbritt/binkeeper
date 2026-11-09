"""Synthetic process-failure probes; these do not simulate physical power loss."""

import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest

from binkeeper.blob_vault import (
    BlobVaultError,
    FilesystemBlobStore,
    content_object_key,
    open_blob,
    put_blob,
)

DATA = b"Synthetic immutable vault original"
KEY = b"k" * 32


@pytest.mark.parametrize(
    "point,published,committed",
    [
        ("before_write", False, False),
        ("before_publish", False, False),
        ("after_publish", True, False),
        ("before_commit", True, False),
        ("after_commit", True, True),
    ],
)
def test_crash_windows_and_explicit_retry(conn, tmp_path, point, published, committed):
    root = tmp_path / "vault"
    script = """
import os, sys, psycopg
from binkeeper.blob_vault import FilesystemBlobStore, put_blob
root, point = sys.argv[1:]
store = FilesystemBlobStore(root)
original_put, original_replace = store.put, os.replace
def replace(*args):
    if point == 'before_publish': os._exit(23)
    original_replace(*args)
def put(*args):
    if point == 'before_write': os._exit(23)
    original_put(*args)
    if point == 'after_publish': os._exit(23)
os.replace, store.put = replace, put
with psycopg.connect(os.environ['BINKEEPER_TEST_DATABASE_URL']) as conn:
    with conn.transaction():
        data = b'Synthetic immutable vault original'
        put_blob(conn, store, data, key=b'k'*32, key_ref='synthetic')
        if point == 'before_commit': os._exit(23)
if point == 'after_commit': os._exit(23)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), point],
        env={**os.environ, "BINKEEPER_TEST_DATABASE_URL": conn.info.dsn},
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 23, result.stderr.decode()
    digest = hashlib.sha256(DATA).hexdigest()
    path = root / content_object_key(digest)
    assert path.exists() == published
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (int(committed),)
    store = FilesystemBlobStore(root)
    if committed:
        assert open_blob(conn, store, digest, key=KEY) == DATA
    else:
        with pytest.raises(BlobVaultError, match="no blob"):
            open_blob(conn, store, digest, key=KEY)
    report = {
        "point": point,
        "published": published,
        "metadata": int(committed),
        "plaintext_sha256": digest,
        "ciphertext_sha256": hashlib.sha256(path.read_bytes()).hexdigest() if published else None,
    }
    retry = put_blob(conn, store, DATA, key=KEY, key_ref="synthetic")
    assert retry.already_existed == committed
    assert open_blob(conn, store, digest, key=KEY) == DATA
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM capture_evidence").fetchone() == (0,)
    print(json.dumps(report))


@pytest.mark.parametrize("fault", ["missing", "corrupt"])
def test_retry_never_reports_a_missing_or_corrupt_existing_original_saved(conn, tmp_path, fault):
    store = FilesystemBlobStore(tmp_path)
    saved = put_blob(conn, store, DATA, key=KEY, key_ref="synthetic")
    path = tmp_path / saved.object_key
    original_ciphertext = path.read_bytes()
    if fault == "missing":
        path.unlink()
    else:
        path.write_bytes(b"synthetic damaged ciphertext")
    with pytest.raises(BlobVaultError):
        open_blob(conn, store, saved.plaintext_sha256, key=KEY)
    with pytest.raises(BlobVaultError):
        put_blob(conn, store, DATA, key=KEY, key_ref="synthetic")
    # Operator recovery uses the exact authenticated original, not new metadata.
    path.write_bytes(original_ciphertext)
    assert open_blob(conn, store, saved.plaintext_sha256, key=KEY) == DATA
    assert put_blob(conn, store, DATA, key=KEY, key_ref="synthetic").already_existed
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (1,)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_flush_failure_cannot_report_a_successful_save(conn, tmp_path, monkeypatch, kind):
    original = os.fsync

    def fail(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if is_directory == (kind == "directory"):
            raise OSError("synthetic storage flush failure")
        original(fd)

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="flush failure"):
        put_blob(conn, FilesystemBlobStore(tmp_path), DATA, key=KEY, key_ref="synthetic")
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (0,)


def test_concurrent_autocommit_saves_preserve_one_readable_ciphertext(conn, tmp_path):
    barrier = Barrier(4)

    def save(_):
        with psycopg.connect(conn.info.dsn, autocommit=True) as writer:
            barrier.wait(timeout=5)
            return put_blob(
                writer, FilesystemBlobStore(tmp_path), DATA, key=KEY, key_ref="synthetic"
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(save, range(4)))
    assert sum(not result.already_existed for result in results) == 1
    assert (
        open_blob(conn, FilesystemBlobStore(tmp_path), results[0].plaintext_sha256, key=KEY) == DATA
    )
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (1,)
