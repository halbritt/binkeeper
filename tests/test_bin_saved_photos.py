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

    def list_bins(self) -> Sequence[tuple[str, str]]:
        return [("AGR-014", "My tools")]


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
    assert page.text.count('type="checkbox" name="photo_ids"') == 12
    assert 'href="/saved-photos?offset=12"' in page.text
    assert 'href="/manage/AGR-014#label"' in page.text
    assert page.headers["cache-control"] == "private, no-store"
    assert "private original metadata" not in page.text
    assert not photos.load_calls and not vision.calls
    next_page = client.get("/saved-photos?offset=24")
    assert next_page.text.count('type="checkbox" name="photo_ids"') == 7
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


@pytest.mark.parametrize("path", ["/saved-photos/upload", "/saved-photos/attach"])
def test_saved_photo_writes_refuse_missing_or_foreign_origins(path: str) -> None:
    client = TestClient(bin_photo_web.create_app(saved_photo_source=_SavedPhotos()))
    for headers in ({}, {"Origin": "http://evil.example"}):
        assert client.post(path, headers=headers).status_code == 403


def test_saved_photo_upload_rejects_bad_batch_before_storing(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db

    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", InMemoryBlobStore)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"s" * 32, "test"))
    client = TestClient(bin_photo_web.create_app(saved_photo_source=_SavedPhotos()))
    response = client.post(
        "/saved-photos/upload",
        headers=ORIGIN,
        files=[
            ("photos", ("ok.jpg", _jpeg(), "image/jpeg")),
            ("photos", ("bad.jpg", b"not an image", "image/jpeg")),
        ],
    )
    assert response.status_code == 400
    assert "photo is corrupt" in response.text
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (0,)


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


def test_saved_photo_prefilled_label_is_kept_while_contents_are_inferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()
    vision = _Vision()
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).post(
        "/saved-photos",
        headers=ORIGIN,
        data={
            "photo_ids": PHOTO_IDS[0],
            "site": "alameda-garage",
            "bin_code": "AGR-123",
            "theme": "MY WORKBENCH",
        },
    )
    assert response.status_code == 200
    assert 'value="MY WORKBENCH"' in response.text
    assert "hex keys" in response.text
    assert "Hand tools" not in response.text
    assert "Do not propose a theme" in vision.calls[0][0]


def test_saved_photo_prefilled_contents_are_kept_while_label_is_inferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()
    vision = _Vision()
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).post(
        "/saved-photos",
        headers=ORIGIN,
        data={
            "photo_ids": PHOTO_IDS[0],
            "site": "alameda-garage",
            "bin_code": "AGR-123",
            "contents": "2 calipers\nM3 screws & washers",
        },
    )
    assert response.status_code == 200
    assert 'value="Hand tools"' in response.text
    assert "2 calipers\nM3 screws &amp; washers</textarea>" in response.text
    assert "hex keys" not in response.text
    assert "Do not list or revise the contents" in vision.calls[0][0]


def test_saved_photo_with_both_fields_prefilled_skips_vision_and_goes_to_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()

    def no_model() -> _Vision:
        pytest.fail("Both fields were supplied; no model should be configured or called")

    monkeypatch.setattr(bin_vision, "default_vision_client", no_model)
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).post(
        "/saved-photos",
        headers=ORIGIN,
        data={
            "photo_ids": PHOTO_IDS[0],
            "site": "alameda-garage",
            "bin_code": "AGR-123",
            "theme": "MY WORKBENCH",
            "contents": "calipers; M3 washers",
        },
    )
    assert response.status_code == 200
    assert 'value="MY WORKBENCH"' in response.text
    assert "calipers; M3 washers</textarea>" in response.text
    assert "Both fields were supplied" in response.text
    assert 'action="/register/confirm"' in response.text


@pytest.mark.parametrize("field,value", [("theme", "MY WORKBENCH"), ("contents", "My calipers")])
def test_prefilled_field_survives_model_failure(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: _Vision(fail=True))
    response = TestClient(bin_photo_web.create_app(saved_photo_source=_SavedPhotos())).post(
        "/saved-photos",
        headers=ORIGIN,
        data={"photo_ids": PHOTO_IDS[0], "bin_code": "AGR-123", field: value},
    )
    assert response.status_code == 200
    assert value in response.text
    assert "Synthetic model outage" in response.text


@pytest.mark.parametrize("route", ["/", "/saved-photos"])
@pytest.mark.parametrize(
    "client_type", [bin_vision.OpenRouterVisionClient, bin_vision.GeminiVisionClient]
)
@pytest.mark.parametrize("field,value", [("theme", "MY WORKBENCH"), ("contents", "My calipers")])
def test_image_preparation_refusal_keeps_originals_and_manual_review(
    monkeypatch, route, client_type, field, value
) -> None:
    photos = _SavedPhotos()
    original = photos.image
    digest = hashlib.sha256(original).hexdigest()
    stored: list[bytes] = []
    transport_calls: list[object] = []

    def store(images):
        stored.extend(images)
        return [digest], None

    def fail_preparation(*args, **kwargs):
        raise bin_vision.ImagePreparationError("Cannot prepare image: synthetic transform failure")

    def transport(*args, **kwargs):
        transport_calls.append(args)
        pytest.fail("Preparation refusal must precede transport")

    monkeypatch.setattr(bin_photo_web, "_store_photos", store)
    monkeypatch.setattr(bin_photo_web, "_harvest_colocations", lambda images: None)
    monkeypatch.setattr(
        bin_vision, "default_vision_client", lambda: client_type(api_key="synthetic-key")
    )
    monkeypatch.setattr(bin_vision, "_prepare_image", fail_preparation)
    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", transport)
    form = {"bin_code": "AGR-123", "site": "alameda-garage", field: value}
    if route == "/saved-photos":
        form["photo_ids"] = PHOTO_IDS[0]
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).post(
        route,
        headers=ORIGIN,
        data=form,
        files={"photos": ("original.jpg", original, "image/jpeg")} if route == "/" else None,
    )

    assert response.status_code == 200
    assert "Cannot prepare image" in response.text
    assert value in response.text
    assert 'value="AGR-123"' in response.text
    assert 'value="alameda-garage" selected' in response.text
    assert f'name="photo_sha256" value="{digest}"' in response.text
    assert 'action="/register/confirm"' in response.text
    assert stored == ([original] if route == "/" else [])
    assert photos.image == original
    assert not transport_calls


def test_prefilled_fields_survive_vault_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    photos = _SavedPhotos()

    def unavailable(photo_ids: Sequence[str]) -> Sequence[SavedPhotoContent]:
        raise SavedPhotosUnavailableError("synthetic vault outage")

    monkeypatch.setattr(photos, "load_photos", unavailable)
    response = TestClient(bin_photo_web.create_app(saved_photo_source=photos)).post(
        "/saved-photos",
        headers=ORIGIN,
        data={"photo_ids": PHOTO_IDS[0], "theme": "MY WORKBENCH", "contents": "My calipers"},
    )
    assert response.status_code == 503
    assert 'value="MY WORKBENCH"' in response.text
    assert "My calipers</textarea>" in response.text


@pytest.mark.parametrize(
    "theme,contents",
    [
        ("", ""),
        ("MY WORKBENCH", ""),
        ("", "My calipers"),
        ("MY WORKBENCH", "My calipers"),
    ],
)
def test_new_uploads_use_the_same_prefill_rules(
    monkeypatch: pytest.MonkeyPatch,
    theme: str,
    contents: str,
) -> None:
    vision = _Vision()
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    monkeypatch.setattr(bin_photo_web, "_store_photos", lambda images: (["a" * 64], None))
    monkeypatch.setattr(bin_photo_web, "_harvest_colocations", lambda images: None)
    response = TestClient(bin_photo_web.create_app()).post(
        "/",
        headers=ORIGIN,
        files={"photos": ("test.jpg", _jpeg(), "image/jpeg")},
        data={"theme": theme, "contents": contents, "bin_code": "AGR-123"},
    )
    assert response.status_code == 200
    assert f'value="{theme or "Hand tools"}"' in response.text
    assert f">{contents or 'hex keys'}</textarea>" in response.text
    assert bool(vision.calls) == (not (theme and contents))


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


def test_saved_photo_upload_stores_original_without_analysis_or_inventory_changes(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db

    store = InMemoryBlobStore()
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"s" * 32, "synthetic"))

    def forbidden() -> _Vision:
        pytest.fail("Saving photos must not call vision")

    monkeypatch.setattr(bin_vision, "default_vision_client", forbidden)
    client = TestClient(bin_photo_web.create_app(saved_photo_source=_SavedPhotos()))
    response = client.post(
        "/saved-photos/upload",
        headers=ORIGIN,
        files={"photos": ("test.jpg", _jpeg(), "image/jpeg")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/saved-photos"
    assert conn.execute("SELECT plaintext_sha256 FROM evidence_blobs").fetchall() == [
        (hashlib.sha256(_jpeg()).hexdigest(),)
    ]
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM colocation_observations").fetchone() == (0,)


def test_saved_photos_can_be_attached_to_existing_bin_without_analysis(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    photos = _SavedPhotos()
    store = InMemoryBlobStore()
    put_blob(conn, store, photos.image, key=b"s" * 32, key_ref="synthetic", content_type="image")
    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="My tools",
        contents_text="My contents",
    )
    monkeypatch.setattr(bin_saved_photos, "connect", lambda **_: nullcontext(conn))

    def forbidden() -> _Vision:
        pytest.fail("Attaching saved photos must not call vision")

    monkeypatch.setattr(bin_vision, "default_vision_client", forbidden)
    client = TestClient(bin_photo_web.create_app(saved_photo_source=photos))
    for _ in range(2):
        response = client.post(
            "/saved-photos/attach",
            headers=ORIGIN,
            data={
                "photo_ids": PHOTO_IDS[0],
                "target_bin_code": "AGR-014",
            },
        )
        assert response.status_code == 200
        assert "Selected photos are attached to AGR-014" in response.text
    assert conn.execute(
        "SELECT count(*) FROM captures WHERE raw_payload->'metadata' ? 'photo'"
    ).fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM colocation_observations").fetchone() == (0,)


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
