"""Read saved uploads for an owner-requested label retry, without rewriting evidence."""

from __future__ import annotations

import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

import psycopg

from binkeeper.bin_photo_media import MAX_LABEL_PHOTOS as MAX_LABEL_PHOTOS
from binkeeper.blob_vault import (
    BlobVaultError,
    blob_store_from_config,
    open_blob,
    vault_key_from_config,
)
from binkeeper.database import ServingRoleUnavailableError, connect

PAGE_SIZE = 12


class SavedPhotoSelectionError(ValueError):
    """The requested photo selection cannot be used."""


class SavedPhotosUnavailableError(RuntimeError):
    """The configured database or vault could not supply the saved photos."""


@dataclass(frozen=True)
class SavedPhoto:
    photo_id: str
    saved_at: datetime
    bin_code: str | None


@dataclass(frozen=True)
class SavedPhotoContent:
    sha256: str
    image: bytes


class SavedPhotoSource(Protocol):
    def list_photos(self, *, offset: int, limit: int) -> Sequence[SavedPhoto]: ...

    def load_photos(self, photo_ids: Sequence[str]) -> Sequence[SavedPhotoContent]: ...

    def list_bins(self) -> Sequence[tuple[str, str]]: ...


def selected_photo_ids(values: Sequence[str]) -> list[str]:
    try:
        ids = list(dict.fromkeys(str(UUID(value)) for value in values))
    except ValueError as exc:
        raise SavedPhotoSelectionError("Choose photos from the saved-photos list.") from exc
    if not 1 <= len(ids) <= MAX_LABEL_PHOTOS:
        raise SavedPhotoSelectionError(
            f"Select between 1 and {MAX_LABEL_PHOTOS} photos of one bin."
        )
    return ids


class ConfiguredSavedPhotos:
    def __init__(self, *, tenant_id: str, corpus_id: str) -> None:
        self.tenant_id = tenant_id
        self.corpus_id = corpus_id

    def list_bins(self) -> list[tuple[str, str]]:
        from binkeeper.bin_passport import load_bin_passports

        try:
            with connect(role="serving") as conn:
                return [
                    (bin.bin_code, bin.theme or "No label")
                    for bin in load_bin_passports(
                        conn,
                        tenant_id=self.tenant_id,
                        corpus_id=self.corpus_id,
                    )
                ]
        except (ServingRoleUnavailableError, psycopg.Error) as exc:
            raise SavedPhotosUnavailableError("Could not load the existing bins.") from exc

    def list_photos(self, *, offset: int, limit: int) -> list[SavedPhoto]:
        try:
            with connect(role="serving") as conn:
                rows = conn.execute(
                    """
                    SELECT b.id, b.created_at, (
                        SELECT c.raw_payload->'metadata'->>'bin_code'
                        FROM captures c
                        WHERE c.tenant_id = b.tenant_id AND c.corpus_id = b.corpus_id
                          AND c.raw_payload->'metadata'->>'kind' = 'bin_capture'
                          AND COALESCE(c.raw_payload->'metadata'->'photo'->>'blob_ref',
                                       c.raw_payload->'metadata'->'photo'->>'sha256')
                              = b.plaintext_sha256
                        ORDER BY c.observed_at DESC, c.imported_at DESC, c.external_id DESC
                        LIMIT 1
                    )
                    FROM evidence_blobs b
                    WHERE b.tenant_id = %s AND b.corpus_id = %s
                      AND (b.content_type = 'image' OR b.content_type LIKE 'image/%%')
                    ORDER BY b.created_at DESC, b.seq DESC
                    LIMIT %s OFFSET %s
                    """,
                    (self.tenant_id, self.corpus_id, limit, offset),
                ).fetchall()
        except (ServingRoleUnavailableError, psycopg.Error) as exc:
            raise SavedPhotosUnavailableError("Could not load saved photos. Try again.") from exc
        return [SavedPhoto(str(row[0]), row[1], row[2]) for row in rows]

    def load_photos(self, photo_ids: Sequence[str]) -> list[SavedPhotoContent]:
        ids = selected_photo_ids(photo_ids)
        try:
            with connect(role="serving") as conn:
                rows = conn.execute(
                    """
                    SELECT id, plaintext_sha256 FROM evidence_blobs
                    WHERE tenant_id = %s AND corpus_id = %s AND id = ANY(%s::uuid[])
                      AND (content_type = 'image' OR content_type LIKE 'image/%%')
                    """,
                    (self.tenant_id, self.corpus_id, ids),
                ).fetchall()
                hashes = {str(row[0]): str(row[1]) for row in rows}
                if len(hashes) != len(ids):
                    raise SavedPhotoSelectionError(
                        "A selected photo is unavailable. Choose from the saved-photos list."
                    )
                store = blob_store_from_config()
                key, _key_ref = vault_key_from_config()
                return [
                    SavedPhotoContent(
                        hashes[photo_id],
                        open_blob(
                            conn,
                            store,
                            hashes[photo_id],
                            key=key,
                            tenant_id=self.tenant_id,
                            corpus_id=self.corpus_id,
                        ),
                    )
                    for photo_id in ids
                ]
        except (
            BlobVaultError,
            binascii.Error,
            json.JSONDecodeError,
            OSError,
            ServingRoleUnavailableError,
            psycopg.Error,
        ) as exc:
            raise SavedPhotosUnavailableError(
                "Could not read the saved photos from the vault. Try again."
            ) from exc


def attach_photos_to_bin(
    photos: Sequence[SavedPhotoContent],
    bin_code: str,
    *,
    tenant_id: str,
    corpus_id: str,
) -> None:
    from binkeeper.bin_manage import BinActionScope, append_saved_bin_photos
    from binkeeper.bin_photo_media import validate_bin_photo

    for photo in photos:
        validate_bin_photo(photo.image)
    with connect() as conn:
        append_saved_bin_photos(
            conn,
            bin_code,
            [photo.sha256 for photo in photos],
            scope=BinActionScope(tenant_id, corpus_id),
        )
