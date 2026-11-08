"""Recovery from saved uploads uses scoped originals and never replays evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from binkeeper import bin_photo_web, bin_saved_photos, bin_vision
from binkeeper.bin_register import register_bin
from binkeeper.bin_saved_photos import (
    ConfiguredSavedPhotos,
    SavedPhoto,
    SavedPhotoContent,
    SavedPhotoSelectionError,
    SavedPhotosUnavailableError,
    selected_photo_ids,
)
from binkeeper.blob_vault import InMemoryBlobStore, put_blob
from binkeeper.database import ServingRoleUnavailableError

ORIGIN = {"Origin": "http://testserver:8765"}
PHOTO_IDS = [str(UUID(int=i + 1)) for i in range(31)]


def _jpeg() -> bytes:
    output = BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "private original metadata"
    Image.new("RGB", (1600, 1200), "navy").save(output, format="JPEG", exif=exif)
    return output.getvalue()


class _SavedPhotos:
    def __init__(self, count: int = 31) -> None:
        self.rows = [
            SavedPhoto(
                photo_id, datetime(2026, 11, 8, 16, 39, tzinfo=UTC), "AGR-014" if i == 0 else None
            )
            for i, photo_id in enumerate(PHOTO_IDS[:count])
        ]
        self.load_calls: list[list[str]] = []
        self.image = _jpeg()

    def list_photos(self, *, offset: int, limit: int) -> Sequence[SavedPhoto]:
        return self.rows[offset : offset + limit]

    def load_photos(self, photo_ids: Sequence[str]) -> Sequence[SavedPhotoContent]:
        self.load_calls.append(list(photo_ids))
        if any(photo_id not in PHOTO_IDS for photo_id in photo_ids):
            raise SavedPhotoSelectionError("A selected photo is unavailable.")
        return [
            SavedPhotoContent(hashlib.sha256(self.image).hexdigest(), self.image)
            for photo_id in photo_ids
        ]


class _Vision:
    model = "synthetic-recovery-model"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.fail = fail

    def analyze(self, prompt: str, image: bytes) -> str:
        self.calls.append((prompt, image))
        if self.fail:
            raise bin_vision.BinVisionError("Synthetic model outage")
        return json.dumps(
            {
                "theme": "Hand tools",
                "items": [{"label": "hex keys", "confidence": 0.9, "traits": []}],
                "summary": "Tools",
            }
        )


def _forbid_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Saved-photo review must not store, register, harvest, or print")

    for name in ("_store_photos", "_harvest_colocations", "_register_and_view"):
        monkeypatch.setattr(bin_photo_web, name, forbidden)


def test_saved_gallery_pages_without_loading_originals_or_calling_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_side_effects(monkeypatch)
    vision = _Vision()
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    photos = _SavedPhotos()
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    page = client.get("/saved-photos")
    assert page.status_code == 200
    assert page.text.count('name="photo_ids"') == 12
    assert 'href="/saved-photos?offset=12"' in page.text
    assert 'href="/manage/AGR-014#label"' in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert "private original metadata" not in page.text
    assert not photos.load_calls and not vision.calls
    next_page = client.get("/saved-photos?offset=24")
    assert next_page.text.count('name="photo_ids"') == 7
    assert 'href="/saved-photos?offset=12"' in next_page.text
    assert "Older photos" not in next_page.text
    assert "No saved photos on this page" in client.get("/saved-photos?offset=48").text
    assert client.get("/saved-photos?offset=-1").status_code == 422


def test_saved_thumbnail_strips_metadata_and_refuses_invalid_photo() -> None:
    photos = _SavedPhotos()
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    response = client.get(f"/saved-photos/{PHOTO_IDS[0]}/thumbnail")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "image/jpeg"
    with Image.open(BytesIO(response.content)) as thumbnail:
        assert thumbnail.size == (960, 720)
        assert not thumbnail.getexif()
    for photo_id in ("invalid", str(UUID(int=100))):
        assert client.get(f"/saved-photos/{photo_id}/thumbnail").status_code == 404
    photos.image = b"not an image"
    assert client.get(f"/saved-photos/{PHOTO_IDS[0]}/thumbnail").status_code == 404


def test_saved_photo_routes_require_owner_front_and_retry_origin() -> None:
    photos = _SavedPhotos()
    app = bin_photo_web.create_app(
        saved_photo_source=photos, allowed_paired_origins=(("https", "paired.example"),)
    )
    client = TestClient(app)
    for path in ("/saved-photos", f"/saved-photos/{PHOTO_IDS[0]}/thumbnail"):
        assert client.get(path, headers={"Host": "unpaired.example:8765"}).status_code == 409
        assert (
            client.get(
                path, headers={"Host": "paired.example:8765", "X-Forwarded-Proto": "https"}
            ).status_code
            == 200
        )
    for headers in ({}, {"Origin": "http://evil.example"}):
        assert (
            client.post(
                "/saved-photos", data={"photo_ids": PHOTO_IDS[0]}, headers=headers
            ).status_code
            == 403
        )
    assert photos.load_calls == [[PHOTO_IDS[0]]]


@pytest.mark.parametrize("fail", [False, True])
def test_saved_retry_reuses_original_and_retains_review_on_model_failure(
    monkeypatch: pytest.MonkeyPatch,
    fail: bool,
) -> None:
    _forbid_side_effects(monkeypatch)
    photos = _SavedPhotos()
    vision = _Vision(fail=fail)
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    response = client.post(
        "/saved-photos",
        headers=ORIGIN,
        data={
            "photo_ids": [PHOTO_IDS[1], PHOTO_IDS[0]],
            "site": "alameda-garage",
            "notes": "keep near the workbench",
            "bin_code": "AGR-123",
        },
    )
    assert response.status_code == 200
    assert photos.load_calls == [[PHOTO_IDS[1], PHOTO_IDS[0]]]
    assert vision.calls and vision.calls[0][1] == photos.image
    assert "keep near the workbench" in vision.calls[0][0]
    assert 'value="AGR-123"' in response.text
    assert 'value="alameda-garage" selected' in response.text
    assert (
        f'name="photo_sha256" value="{hashlib.sha256(photos.image).hexdigest()}"' in response.text
    )
    assert 'action="/register/confirm"' in response.text
    assert "Create and print label" in response.text
    assert ("Synthetic model outage" in response.text) == fail
    assert ("hex keys" in response.text) != fail


@pytest.mark.parametrize("selection", [[], ["invalid"], PHOTO_IDS[:13]])
def test_saved_retry_rejects_selection_before_reading_originals(selection: list[str]) -> None:
    photos = _SavedPhotos()
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    response = client.post(
        "/saved-photos?offset=24",
        headers=ORIGIN,
        data={
            "photo_ids": selection,
            "notes": "preserve these notes",
            "site": "alameda-garage",
            "bin_code": "AGR-123",
        },
    )
    assert response.status_code == 400
    assert "preserve these notes" in response.text
    assert 'value="AGR-123"' in response.text
    assert 'action="/saved-photos?offset=24"' in response.text
    assert not photos.load_calls


def test_saved_gallery_reports_outage_instead_of_empty_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()

    def unavailable(**kwargs: object) -> Sequence[SavedPhoto]:
        raise SavedPhotosUnavailableError("private backend detail")

    monkeypatch.setattr(photos, "list_photos", unavailable)
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).get("/saved-photos")
    assert response.status_code == 503
    assert "Could not load saved photos" in response.text
    assert "private backend detail" not in response.text
    assert "No saved photos" not in response.text


def test_saved_retry_retains_selection_and_details_when_vault_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()

    def unavailable(photo_ids: Sequence[str]) -> Sequence[SavedPhotoContent]:
        raise SavedPhotosUnavailableError("private vault detail")

    monkeypatch.setattr(photos, "load_photos", unavailable)
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    response = client.post(
        "/saved-photos",
        headers=ORIGIN,
        data={
            "photo_ids": PHOTO_IDS[0],
            "notes": "keep these together",
            "site": "alameda-garage",
            "bin_code": "AGR-123",
        },
    )
    assert response.status_code == 503
    assert "Could not read the selected photos" in response.text
    assert "private vault detail" not in response.text
    assert f'value="{PHOTO_IDS[0]}" checked' in response.text
    assert "keep these together" in response.text
    assert 'value="alameda-garage" selected' in response.text
    assert 'value="AGR-123"' in response.text


def test_selection_deduplicates_and_keeps_owner_order() -> None:
    assert selected_photo_ids([PHOTO_IDS[1], PHOTO_IDS[0], PHOTO_IDS[1]]) == PHOTO_IDS[1::-1]


def test_configured_source_scopes_originals_and_retry_does_not_write_evidence(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryBlobStore()
    key = b"s" * 32
    source = ConfiguredSavedPhotos(tenant_id="personal", corpus_id="personal")

    def serving_connect(*, role: str) -> object:
        assert role == "serving"
        return nullcontext(conn)

    monkeypatch.setattr(bin_saved_photos, "connect", serving_connect)
    monkeypatch.setattr(bin_saved_photos, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(bin_saved_photos, "vault_key_from_config", lambda: (key, "synthetic"))
    original = _jpeg()
    digest = hashlib.sha256(original).hexdigest()
    put_blob(conn, store, original, key=key, key_ref="synthetic", content_type="image")
    personal_id = str(conn.execute("SELECT id FROM evidence_blobs").fetchone()[0])
    register_bin(conn, bin_code="AGR-014", site="alameda-garage", photo_sha256=digest)
    for tenant_id, corpus_id, content_type in (
        ("other", "personal", "image"),
        ("personal", "other", "image/jpeg"),
        ("personal", "personal", "application/pdf"),
    ):
        put_blob(
            conn,
            store,
            b"synthetic foreign or non-image",
            key=key,
            key_ref="synthetic",
            content_type=content_type,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    assert [
        (photo.photo_id, photo.bin_code) for photo in source.list_photos(offset=0, limit=25)
    ] == [(personal_id, "AGR-014")]
    assert source.load_photos([personal_id]) == [SavedPhotoContent(digest, original)]
    for (foreign_id,) in conn.execute(
        "SELECT id FROM evidence_blobs WHERE id != %s", (personal_id,)
    ):
        with pytest.raises(SavedPhotoSelectionError):
            source.load_photos([personal_id, str(foreign_id)])

    def evidence_counts() -> tuple[int, ...]:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "evidence_blobs",
                "capture_evidence",
                "bin_trip_events",
                "colocation_observations",
            )
        )

    before = evidence_counts()
    _forbid_side_effects(monkeypatch)
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: _Vision())
    response = TestClient(bin_photo_web.create_app(saved_photo_source=source)).post(
        "/saved-photos",
        headers=ORIGIN,
        data={"photo_ids": personal_id, "site": "alameda-garage", "bin_code": "AGR-123"},
    )
    assert response.status_code == 200 and "hex keys" in response.text
    assert evidence_counts() == before
    monkeypatch.setattr(bin_saved_photos, "vault_key_from_config", lambda: (b"x" * 32, "wrong"))
    with pytest.raises(SavedPhotosUnavailableError):
        source.load_photos([personal_id])


def test_configured_source_reports_serving_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**kwargs: object) -> object:
        raise ServingRoleUnavailableError("private database detail")

    monkeypatch.setattr(bin_saved_photos, "connect", unavailable)
    source = ConfiguredSavedPhotos(tenant_id="personal", corpus_id="personal")
    with pytest.raises(SavedPhotosUnavailableError, match="Could not load saved photos"):
        source.list_photos(offset=0, limit=25)
    with pytest.raises(SavedPhotosUnavailableError, match="Could not read the saved photos"):
        source.load_photos(PHOTO_IDS[:1])
