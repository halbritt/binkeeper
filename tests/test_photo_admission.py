"""Bounded interactive batches keep accepted originals recoverable."""

import hashlib
import json
import logging
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from io import BytesIO
from threading import Event, Lock
from time import perf_counter

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from binkeeper import bin_photo_web, bin_saved_photos, bin_vision, blob_vault, db
from binkeeper.bin_photo_media import MAX_PHOTO_BYTES
from binkeeper.bin_saved_photos import ConfiguredSavedPhotos, SavedPhoto, SavedPhotoContent
from binkeeper.interactive_vision import VisionBudget

ORIGIN = {"Origin": "http://testserver:8765"}


class EmptyPhotos:
    def list_photos(self, *, offset: int, limit: int) -> list[SavedPhoto]:
        return []

    def list_bins(self) -> list[tuple[str, str]]:
        return []

    def load_photos(self, photo_ids: Sequence[str]) -> list[SavedPhotoContent]:
        pytest.fail("Invalid batch should not load a saved photo")


def jpeg(seed=0):
    out = BytesIO()
    Image.new("RGB", (64, 64), (seed, 40, 80)).save(out, format="JPEG", quality=100)
    return out.getvalue()


def test_two_overlapping_thirteen_photo_requests_make_no_model_calls(monkeypatch):
    monkeypatch.setattr(
        bin_vision, "default_vision_client", lambda: pytest.fail("Over-limit batch analyzed")
    )
    monkeypatch.setattr(
        bin_photo_web, "_store_photos", lambda *_: pytest.fail("Over-limit batch stored")
    )
    client = TestClient(bin_photo_web.create_app())

    def submit(_):
        return client.post(
            "/",
            headers=ORIGIN,
            data={"theme": "MY TOOLS"},
            files=[("photos", ("test.jpg", jpeg(), "image/jpeg"))] * 13,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, range(2)))
    assert all(response.status_code == 400 for response in responses)
    assert all("MY TOOLS" in response.text for response in responses)


@pytest.mark.parametrize("path", ["/", "/saved-photos/upload"])
@pytest.mark.parametrize("problem", ["count", "size", "invalid", "empty"])
def test_invalid_batches_never_reach_storage_or_model(monkeypatch, path, problem):
    monkeypatch.setattr(
        bin_photo_web, "_store_photos", lambda *_: pytest.fail("Invalid upload stored")
    )
    monkeypatch.setattr(
        bin_vision, "default_vision_client", lambda: pytest.fail("Invalid upload analyzed")
    )
    data = {
        "theme": "MY LABEL",
        "contents": "My contents",
        "bin_code": "AGR-123",
        "site": "alameda-garage",
    }
    payload = {"size": b"x" * (MAX_PHOTO_BYTES + 1), "invalid": b"not a photo", "empty": b""}.get(
        problem, jpeg()
    )
    files = [
        ("photos", (f"{n}.jpg", payload, "image/jpeg"))
        for n in range(13 if problem == "count" else 1)
    ]
    empty_source = EmptyPhotos()
    response = TestClient(bin_photo_web.create_app(saved_photo_source=empty_source)).post(
        path, data=data, files=files, headers=ORIGIN
    )
    assert response.status_code == 400
    if path == "/":
        for text in (
            "MY LABEL",
            "My contents",
            'value="AGR-123"',
            'value="alameda-garage" selected',
        ):
            assert text in response.text


def test_overlap_cannot_multiply_budget_and_saved_photos_remain_recoverable(
    conn, monkeypatch, caplog
):
    store = blob_vault.InMemoryBlobStore()
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"k" * 32, "synthetic"))
    monkeypatch.setattr(bin_saved_photos, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(bin_saved_photos, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(bin_saved_photos, "vault_key_from_config", lambda: (b"k" * 32, "synthetic"))
    monkeypatch.setattr(bin_photo_web, "_harvest_colocations", lambda images: None)
    monkeypatch.setattr(bin_photo_web, "INTERACTIVE_VISION_BUDGET", VisionBudget(3))
    vision = bin_vision.OpenRouterVisionClient(api_key="synthetic")
    entered, release, lock = Event(), Event(), Lock()
    active = peak = calls = 0

    def analyze(prompt, image):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 3:
                entered.set()
        try:
            assert release.wait(timeout=10), "test did not release synthetic model"
            return json.dumps(
                {"theme": "Tools", "items": [{"label": "hex keys", "confidence": 0.9}]}
            )
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(vision, "analyze", analyze)
    monkeypatch.setattr(bin_vision, "default_vision_client", lambda: vision)
    client = TestClient(bin_photo_web.create_app())
    source = ConfiguredSavedPhotos(tenant_id="personal", corpus_id="personal")
    images = [jpeg(n * 10) for n in range(12)]
    fields = {"theme": "MY TOOLS", "site": "alameda-garage", "bin_code": "AGR-123"}
    caplog.set_level(logging.INFO, logger="binkeeper.bin_photo_web")
    start = perf_counter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(
            client.post,
            "/",
            headers=ORIGIN,
            data=fields,
            files=[("photos", ("test.jpg", image, "image/jpeg")) for image in images],
        )
        try:
            assert entered.wait(timeout=5)
            rejected = client.post(
                "/",
                headers=ORIGIN,
                data=fields,
                files={"photos": ("later.jpg", jpeg(220), "image/jpeg")},
            )
            assert rejected.status_code == 429 and "Photo analysis is busy" in rejected.text
            assert "MY TOOLS" in rejected.text and 'value="AGR-123"' in rejected.text
            assert "1 of 1 photo(s) stored" in rejected.text
            rows = source.list_photos(offset=0, limit=20)
            assert len(rows) == 13
            saved = client.post(
                "/saved-photos", headers=ORIGIN, data={**fields, "photo_ids": rows[0].photo_id}
            )
            assert saved.status_code == 429 and "MY TOOLS" in saved.text
            assert calls == 3 and peak == 3
            # Closing/reopening the client loses no accepted original. Only saved
            # photos, not arbitrary abandoned browser fields, are durable.
            reopened = TestClient(bin_photo_web.create_app())
            assert "Saved photos" in reopened.get("/saved-photos").text
            assert source.load_photos([rows[0].photo_id])[0].image == jpeg(220)
        finally:
            release.set()
        assert first.result(timeout=10).status_code == 200
    retry = client.post(
        "/saved-photos", headers=ORIGIN, data={**fields, "photo_ids": rows[0].photo_id}
    )
    assert retry.status_code == 200 and calls == 13 and peak == 3
    assert conn.execute("SELECT count(*) FROM capture_evidence").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (0,)
    assert (
        blob_vault.open_blob(conn, store, hashlib.sha256(images[0]).hexdigest(), key=b"k" * 32)
        == images[0]
    )
    metrics = [
        json.loads(record.getMessage().partition("BinKeeper photo phases ")[2])
        for record in caplog.records
        if "BinKeeper photo phases " in record.getMessage()
    ]
    assert {record["outcome"] for record in metrics} == {"busy", "completed"}
    assert all(
        all(name in record for name in ("storage_ms", "model_ms", "merge_overhead_ms", "render_ms"))
        for record in metrics
    )
    assert "MY TOOLS" not in json.dumps(metrics)
    print(
        json.dumps(
            {
                "overlap_elapsed_ms": round((perf_counter() - start) * 1000, 2),
                "peak_calls": peak,
                "phases": metrics,
            }
        )
    )
