"""RFC 0088 T4 / RFC 0093 P5 tests for the BinKeeper photo-drop surface.

No live LLM or printer is used. Pure route tests stub their boundaries; existing-bin
action tests use the migrated ``BINKEEPER_TEST_DATABASE_URL`` database.
"""

from __future__ import annotations

import hashlib
from contextlib import nullcontext
from datetime import UTC, datetime
from io import BytesIO

import psycopg
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from binkeeper import bin_photo_web
from binkeeper.bin_inventory import bin_where
from binkeeper.bin_label import PrintPlan
from binkeeper.bin_photo_web import manage as bin_manage_web
from binkeeper.bin_photo_web import stash as bin_stash_web
from binkeeper.bin_register import RegisterResult
from binkeeper.bin_vision import BinLabelProposal, DetectedItem
from binkeeper.blob_vault import InMemoryBlobStore

_LOOPBACK_ORIGIN = {"Origin": "http://testserver:8765", "Sec-Fetch-Site": "same-origin"}


def _jpeg_bytes() -> bytes:
    with Image.new("RGB", (1, 1), color=(70, 110, 150)) as image, BytesIO() as output:
        image.save(output, format="JPEG")
        return output.getvalue()


_ONE_PIXEL_JPEG = _jpeg_bytes()


class _FailingBlobStore:
    backend_name = "failing-test-store"

    def put(self, object_key: str, data: bytes) -> None:
        raise OSError("synthetic store failure")

    def get(self, object_key: str) -> bytes:
        raise AssertionError("get must not be called")

    def exists(self, object_key: str) -> bool:
        return False


def _client() -> TestClient:
    return TestClient(bin_photo_web.create_app(host="127.0.0.1", port=8765))


def _assert_isolated_binkeeper_navigation(body: str, *, current: str) -> None:
    assert "BinKeeper" in body
    assert 'aria-label="BinKeeper"' in body
    assert 'href="/bins/"' in body
    assert 'href="/"' in body
    assert 'href="/register"' in body
    assert f'data-binkeeper-section="{current}" aria-current="page"' in body
    assert ">Interview<" not in body
    assert ">Context quiz<" not in body


def _canned_view() -> dict[str, object]:
    proposal = BinLabelProposal(
        theme="hand tools",
        accepts=("hex keys",),
        owner_phrase="keep near the workbench",
        summary="small hand tools",
        items=(DetectedItem(label="hex keys", traits=(), confidence=0.9),),
        model_version="qwen3-vl:test",
        photo_count=1,
    )
    return {
        "photo_count": 1,
        "blob_refs": ["deadbeef"],
        "store_error": None,
        "proposal": proposal.to_json(),
        "label_error": None,
        "site": "alameda-garage",
        "suggested_bin_code": "AGR-014",
        "all_sites": ["alameda-garage", "alameda-storage", "alameda-home"],
        "proposed_contents": "hex keys",
    }


def _register_result(*, already_existed: bool = False) -> RegisterResult:
    return RegisterResult(
        bin_code="AGR-014",
        site="alameda-garage",
        capture_id="capture-1" if not already_existed else "",
        trip_event_id="place-1" if not already_existed else "",
        already_existed=already_existed,
    )


def test_get_renders_upload_form() -> None:
    resp = _client().get("/")
    assert resp.status_code == 200
    body = resp.text
    assert "Bin photo drop" in body
    assert 'type="file"' in body
    assert 'name="notes"' in body


def test_binkeeper_photo_and_register_pages_have_only_local_navigation() -> None:
    with _client() as client:
        photo = client.get("/")
        register = client.get("/register")

    assert photo.status_code == 200
    _assert_isolated_binkeeper_navigation(photo.text, current="photo")
    assert register.status_code == 200
    _assert_isolated_binkeeper_navigation(register.text, current="register")


def test_manage_bin_page_prefills_current_state_and_exposes_clear_actions() -> None:
    def load_view(*, bin_code: str, tenant_id: str, corpus_id: str) -> bin_manage_web.ManageView:
        assert (bin_code, tenant_id, corpus_id) == ("AGR-014", "personal", "personal")
        return {
            "bin_code": "AGR-014",
            "theme": "Power tools",
            "contents": "cordless drills and impact drivers",
            "home_site": "alameda-garage",
            "current_site": "oakland-fab-east",
            "catalog_photo_url": "/bins/photo/AGR-014",
            "container_code": "",
            "containment_path": (),
            "contained_bin_codes": (),
            "container_options": ("AGR-010", "AGR-020"),
        }

    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        manage_loader=load_view,
    )

    response = TestClient(app).get("/manage/AGR-014")

    assert response.status_code == 200
    body = response.text
    assert "Manage AGR-014" in body
    assert 'name="theme" value="Power tools"' in body
    assert "cordless drills and impact drivers" in body
    assert 'name="home_site"' in body
    assert "Home / return-to site" in body
    assert 'name="current_site"' in body
    assert "Current location" in body
    assert "Put this bin inside another bin" in body
    assert 'name="container_code"' in body
    assert '<option value="AGR-010">AGR-010</option>' in body
    assert "Save changes" in body
    assert "Add photo" in body
    assert "Print another label" in body
    assert 'src="/bins/photo/AGR-014"' in body
    _assert_isolated_binkeeper_navigation(body, current="catalog")


def test_manage_profile_post_appends_reviewed_snapshot_and_redirects(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_passport import bin_passport
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="Painting",
        contents_text="paint rollers",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
    )
    with TestClient(app, base_url="http://testserver:8765") as client:
        response = client.post(
            "/manage/AGR-014/profile",
            data={
                "theme": "Power tools",
                "contents": "drills and impact drivers",
                "home_site": "alameda-garage",
                "action_id": "2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
            },
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/manage/AGR-014?notice=profile-saved"
    passport = bin_passport(conn, "AGR-014")
    assert passport.theme == "Power tools"
    assert passport.sibling_contents == ("drills and impact drivers",)
    assert passport.home_site == "alameda-garage"


def test_manage_profile_write_stays_in_the_selected_corpus(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_passport import bin_passport
    from binkeeper.bin_register import register_bin

    for tenant_id, corpus_id, theme in (
        ("personal", "personal", "Personal tools"),
        ("owner-a", "bins-a", "Scoped tools"),
    ):
        register_bin(
            conn,
            bin_code="AGR-014",
            site="alameda-garage",
            theme=theme,
            observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        tenant_id="owner-a",
        corpus_id="bins-a",
    )

    response = TestClient(app).post(
        "/manage/AGR-014/profile",
        data={
            "theme": "Scoped power tools",
            "contents": "drills",
            "home_site": "alameda-garage",
            "action_id": "d553724b-0f77-4ad0-8880-30a261adfbd3",
        },
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("notice=profile-saved")
    scoped = bin_passport(conn, "AGR-014", tenant_id="owner-a", corpus_id="bins-a")
    personal = bin_passport(conn, "AGR-014")
    assert scoped.theme == "Scoped power tools"
    assert personal.theme == "Personal tools"


def test_manage_profile_refuses_a_bin_outside_the_selected_corpus(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme="Personal tools",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        tenant_id="owner-a",
        corpus_id="bins-a",
    )

    response = TestClient(app).post(
        "/manage/AGR-014/profile",
        data={
            "theme": "Wrong scope",
            "action_id": "456b67be-e075-484c-a319-27f61362ec88",
        },
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("notice=save-failed")
    count = conn.execute(
        """
        SELECT count(*) FROM captures
        WHERE tenant_id = 'owner-a' AND corpus_id = 'bins-a'
        """
    ).fetchone()
    assert count == (0,)


def test_manage_photo_post_links_the_uploaded_photo_and_redirects(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    store = InMemoryBlobStore()
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"k" * 32, "test-key"))
    app = bin_photo_web.create_app(host="127.0.0.1", port=8765, base_path="/bin-photo")
    upload = {"action_id": "e5062bf5-61e0-4f46-b744-b46b1f81a896"}
    with TestClient(app, base_url="http://testserver:8765") as client:
        response = client.post(
            "/manage/AGR-014/photo",
            files={"photo": ("contents.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
            data=upload,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        replay = client.post(
            "/manage/AGR-014/photo",
            files={"photo": ("contents.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
            data=upload,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert replay.status_code == 303
    assert response.headers["location"] == "/bin-photo/manage/AGR-014?notice=photo-added"
    row = conn.execute(
        """
        SELECT raw_payload->'metadata'->'photo'->>'sha256', count(*) OVER ()
        FROM captures
        WHERE raw_payload->'metadata'->>'bin_code' = 'AGR-014'
          AND raw_payload->'metadata'->'photo' IS NOT NULL
        """
    ).fetchone()
    assert row is not None
    assert store.exists(f"sha256/{row[0][:2]}/{row[0]}")
    assert row[1] == 1


def test_manage_photo_validates_the_bin_before_writing_ciphertext(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db

    store = InMemoryBlobStore()
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"k" * 32, "test-key"))

    response = _client().post(
        "/manage/AGR-404/photo",
        files={"photo": ("contents.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"action_id": "1925317b-a3b8-4f80-b601-92f0964dc45a"},
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )

    digest = hashlib.sha256(_ONE_PIXEL_JPEG).hexdigest()
    assert response.headers["location"].endswith("notice=photo-failed")
    assert not store.exists(f"sha256/{digest[:2]}/{digest}")


def test_manage_photo_rejects_bytes_the_catalog_cannot_render(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    store = InMemoryBlobStore()
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", lambda: store)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"k" * 32, "test-key"))

    response = _client().post(
        "/manage/AGR-014/photo",
        files={"photo": ("broken.jpg", b"not an image", "image/jpeg")},
        data={"action_id": "eb16a2ec-d812-43de-9007-b50e3c087eb8"},
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )

    photo_capture_count = conn.execute(
        """
        SELECT count(*) FROM captures
        WHERE raw_payload->'metadata'->>'bin_code' = 'AGR-014'
          AND raw_payload->'metadata'->'photo' IS NOT NULL
        """
    ).fetchone()
    digest = hashlib.sha256(b"not an image").hexdigest()
    assert response.headers["location"].endswith("notice=photo-failed")
    assert photo_capture_count == (0,)
    assert not store.exists(f"sha256/{digest[:2]}/{digest}")


def test_manage_photo_rolls_back_its_link_when_blob_storage_fails(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import blob_vault, db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(blob_vault, "blob_store_from_config", _FailingBlobStore)
    monkeypatch.setattr(blob_vault, "vault_key_from_config", lambda: (b"k" * 32, "test-key"))

    response = _client().post(
        "/manage/AGR-014/photo",
        files={"photo": ("contents.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"action_id": "5c93655e-0056-4086-a18e-ce3595ebc9e4"},
        headers=_LOOPBACK_ORIGIN,
        follow_redirects=False,
    )

    photo_capture_count = conn.execute(
        """
        SELECT count(*) FROM captures
        WHERE raw_payload->'metadata'->>'bin_code' = 'AGR-014'
          AND raw_payload->'metadata'->'photo' IS NOT NULL
        """
    ).fetchone()
    assert response.headers["location"].endswith("notice=photo-failed")
    assert photo_capture_count == (0,)


def test_manage_location_post_records_a_placement_and_redirects(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))

    placement = {
        "current_site": "oakland-fab-east",
        "action_id": "c32d9090-2270-4590-901d-b44c253c9113",
    }
    with _client() as client:
        response = client.post(
            "/manage/AGR-014/location",
            data=placement,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        replay = client.post(
            "/manage/AGR-014/location",
            data=placement,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert replay.status_code == 303
    assert response.headers["location"] == "/manage/AGR-014?notice=location-saved"
    assert bin_where(conn, "AGR-014").site == "oakland-fab-east"
    count = conn.execute(
        """
        SELECT count(*) FROM bin_trip_events
        WHERE source_label = 'binkeeper-manage' AND bin_code = 'AGR-014'
        """
    ).fetchone()
    assert count == (1,)


def test_manage_containment_post_packs_once_and_redirects(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_inventory import bin_where
    from binkeeper.bin_register import register_bin

    when = datetime(2026, 7, 30, 6, tzinfo=UTC)
    for code in ("AGR-001", "AGR-010"):
        register_bin(conn, bin_code=code, site="alameda-garage", observed_at=when)
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    action = {
        "action": "pack",
        "container_code": "AGR-010",
        "action_id": "a2e88b6a-451d-4aad-a8c3-2130b3515c84",
    }

    with _client() as client:
        response = client.post(
            "/manage/AGR-001/containment",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        replay = client.post(
            "/manage/AGR-001/containment",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        page = client.get("/manage/AGR-001")
        container_page = client.get("/manage/AGR-010")

    assert response.status_code == 303
    assert response.headers["location"].endswith("notice=containment-packed")
    assert replay.headers["location"].endswith("notice=containment-packed")
    assert bin_where(conn, "AGR-001").container_code == "AGR-010"
    assert conn.execute("SELECT count(*) FROM bin_containment_events").fetchone() == (1,)
    assert "Take out of AGR-010" in page.text
    assert "Move AGR-010 instead" in page.text
    assert "Contains AGR-001" in container_page.text


def test_manage_reprint_post_sends_one_deliberate_label_and_redirects(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, db
    from binkeeper.bin_manage import (
        BinPlacement,
        BinProfileUpdate,
        place_existing_bin,
        update_bin_profile,
    )
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    update_bin_profile(
        conn,
        BinProfileUpdate(
            bin_code="AGR-014",
            theme="Power tools",
            contents="cordless drills and impact drivers",
            home_site="alameda-garage",
            action_id="2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
            observed_at=datetime(2026, 7, 14, 10, tzinfo=UTC),
        ),
    )
    place_existing_bin(
        conn,
        BinPlacement(
            bin_code="AGR-014",
            site="oakland-fab-east",
            action_id="c32d9090-2270-4590-901d-b44c253c9113",
            occurred_at=datetime(2026, 7, 14, 11, tzinfo=UTC),
        ),
    )
    sent: list[tuple[str, str | None]] = []
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    monkeypatch.setattr(
        bin_label,
        "send_to_printer",
        lambda tspl, *, cups_queue: sent.append((tspl, cups_queue)),
    )

    action = {"action_id": "3a581aa2-589e-4626-a31b-62124a193917"}
    with _client() as client:
        response = client.post(
            "/manage/AGR-014/print",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        replay = client.post(
            "/manage/AGR-014/print",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/manage/AGR-014?notice=label-sent"
    assert replay.headers["location"] == "/manage/AGR-014?notice=label-replayed"
    assert len(sent) == 1
    tspl, queue = sent[0]
    assert queue == "OmezizyD450"
    assert "POWER TOOLS" in tspl
    assert "cordless drills and impact drivers" in tspl
    assert "ALA-GARAGE" in tspl
    assert "OAK-FAB-EAST" not in tspl
    assert "PRINT 1,1" in tspl


@pytest.mark.parametrize(
    ("failure_kind", "expected_notice", "expected_message"),
    [
        ("timeout", "print-unknown", "Print status is unknown"),
        ("rejected", "print-failed", "label could not be requested"),
    ],
)
def test_reprint_failure_keeps_the_intent_reserved_without_a_second_attempt(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
    expected_notice: str,
    expected_message: str,
) -> None:
    import subprocess

    from binkeeper import bin_label, db
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    attempts = 0

    def fail(_tspl: str, *, cups_queue: str) -> None:
        nonlocal attempts
        attempts += 1
        if failure_kind == "timeout":
            raise bin_label.BinLabelError("print handoff timed out") from subprocess.TimeoutExpired(
                ["lp", "-d", cups_queue], 15
            )
        raise bin_label.BinLabelError("printer rejected the job")

    monkeypatch.setattr(bin_label, "send_to_printer", fail)

    action = {"action_id": "3a581aa2-589e-4626-a31b-62124a193917"}
    with _client() as client:
        response = client.post(
            "/manage/AGR-014/print",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        replay = client.post(
            "/manage/AGR-014/print",
            data=action,
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        notice = client.get(response.headers["location"])

    intent_count = conn.execute(
        """
        SELECT count(*) FROM captures
        WHERE raw_payload->'metadata'->>'kind' = 'bin_label_print_intent'
        """
    ).fetchone()
    assert response.headers["location"].endswith(f"notice={expected_notice}")
    assert replay.headers["location"].endswith("notice=label-replayed")
    assert attempts == 1
    assert intent_count == (1,)
    assert expected_message in notice.text


@pytest.mark.parametrize(
    ("path", "data"),
    [
        (
            "/manage/AGR-014/profile",
            {
                "action_id": "2d21c8a9-b984-4469-aa21-bbc6b9b6ab94",
            },
        ),
        (
            "/manage/AGR-014/location",
            {
                "action_id": "c32d9090-2270-4590-901d-b44c253c9113",
            },
        ),
        (
            "/manage/AGR-014/containment",
            {
                "action": "pack",
                "container_code": "AGR-099",
                "action_id": "2362f86d-cff6-4a1f-85e8-c6c796a6389c",
            },
        ),
        (
            "/manage/AGR-014/photo",
            {
                "action_id": "e5062bf5-61e0-4f46-b744-b46b1f81a896",
            },
        ),
        (
            "/manage/AGR-014/print",
            {
                "action_id": "3a581aa2-589e-4626-a31b-62124a193917",
            },
        ),
    ],
)
def test_manage_writes_require_an_explicit_allowed_origin(
    path: str,
    data: dict[str, str],
) -> None:
    response = _client().post(path, data=data, follow_redirects=False)

    assert response.status_code == 403


def test_photo_drop_submit_exposes_pending_feedback() -> None:
    # Regression 2026-07-13: submitting photos gave no visible indication that
    # upload and local vision work had started, so duplicate presses felt necessary.
    resp = _client().get("/")

    assert resp.status_code == 200
    body = resp.text
    assert "data-submit-form" in body
    assert 'data-busy-label="Analyzing photos…"' in body
    assert "data-submit-progress hidden" in body
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body
    assert "Photos are uploading." in body


def test_post_renders_proposal(monkeypatch) -> None:
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"notes": "keep near the workbench", "site": "alameda-garage"},
        headers=_LOOPBACK_ORIGIN,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "hand tools" in body
    assert "AGR-014" in body
    assert "hex keys" in body
    assert "stored" in body  # storage is reported


def test_proposal_offers_create_action(monkeypatch) -> None:
    # The bug: the proposal page dead-ended with no way to accept it. It must now
    # offer an "OK / create this bin" action wired to the registration confirm route,
    # pre-filled with the proposed code, site, and contents.
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"notes": "keep near the workbench", "site": "alameda-garage"},
        headers=_LOOPBACK_ORIGIN,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "create this bin" in body.lower()
    assert 'action="/register/confirm"' in body
    assert 'value="AGR-014"' in body  # code pre-filled
    assert 'value="deadbeef"' in body  # the stored photo carried as evidence
    assert '<option value="alameda-garage" selected>' in body  # site pre-selected


def test_proposal_offers_reviewed_create_and_print_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression 2026-07-13: the photo workflow stopped after registration and
    # required reconstructing a separate CLI command before a label could print.
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())

    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    body = resp.text
    assert 'name="theme"' in body
    assert 'value="hand tools"' in body
    assert 'name="print_label" value="1"' in body
    assert "Create and print label" in body
    assert "Create without printing" in body
    assert body.index('name="print_label" value="0"') < body.index('name="print_label" value="1"')


def test_proposal_offers_one_or_two_label_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())

    response = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )

    assert response.status_code == 200
    body = response.text
    assert "Labels to print" in body
    assert 'type="radio" name="label_count" value="1" checked' in body
    assert 'type="radio" name="label_count" value="2"' in body
    assert "1 label" in body
    assert "2 labels" in body


def test_proposal_offers_label_alignment_beside_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())

    response = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )

    assert response.status_code == 200
    body = response.text
    assert 'type="button" data-align-label data-align-action="/printer/align"' in body
    assert "Align label" in body
    assert body.index("Align label") < body.index("Create and print label")
    assert 'id="label-align-status"' in body


def test_align_label_route_advances_one_label_without_printing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label

    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    submissions: list[tuple[str, str | None]] = []

    def fake_send(
        data: str,
        *,
        cups_queue: str | None = None,
        **_kwargs: object,
    ) -> PrintPlan:
        submissions.append((data, cups_queue))
        return PrintPlan("cups", cups_queue or "", len(data.encode("utf-8")))

    monkeypatch.setattr(bin_label, "send_to_printer", fake_send)

    response = _client().post("/printer/align", headers=_LOOPBACK_ORIGIN)

    assert response.status_code == 200
    assert response.json() == {
        "status": "aligned",
        "detail": "Label advanced to the next boundary.",
        "target": "OmezizyD450",
    }
    assert submissions == [("SIZE 4,6\r\nFORMFEED\r\n", "OmezizyD450")]


def test_align_label_route_requires_an_explicit_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("printer boundary must not be called")

    monkeypatch.setattr(bin_label, "send_to_printer", fail_if_called)

    response = _client().post("/printer/align")

    assert response.status_code == 403


def test_align_label_timeout_reports_unknown_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    from binkeeper import bin_label

    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")

    def time_out(*_args: object, **_kwargs: object) -> None:
        raise bin_label.BinLabelError("alignment handoff timed out") from subprocess.TimeoutExpired(
            ["lp", "-d", "OmezizyD450"], 15
        )

    monkeypatch.setattr(bin_label, "send_to_printer", time_out)

    response = _client().post("/printer/align", headers=_LOOPBACK_ORIGIN)

    assert response.status_code == 504
    assert response.json() == {
        "status": "unknown",
        "detail": "Alignment status is unknown. Check the label position before trying again.",
    }


def test_proposal_confirmation_exposes_pending_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    body = resp.text
    assert 'data-busy-label="Creating and printing…"' in body
    assert 'data-busy-label="Creating bin…"' in body
    assert "data-submit-progress hidden" in body
    assert "Finishing the reviewed bin action." in body


def test_post_reports_stored_photos_even_when_label_fails(monkeypatch) -> None:
    # The model failed, but the photos are stored: the page must reassure, not alarm.
    error_view = {
        "photo_count": 2,
        "blob_refs": ["aaa", "bbb"],
        "store_error": None,
        "proposal": None,
        "label_error": "vision model returned HTTP 400: context length exceeded",
        "site": None,
        "suggested_bin_code": None,
    }
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: error_view)
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "2 of 2 photo(s) stored" in body
    assert "context length exceeded" in body
    assert "safe in the vault" in body


def test_post_cross_origin_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(bin_photo_web, "_analyze", lambda **_: _canned_view())
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_suggest_bin_code_uses_site_prefix() -> None:
    assert bin_photo_web._suggest_bin_code("nonexistent-site") is None


# --- registration tap ------------------------------------------------------


def test_get_register_renders_form_with_prefilled_code() -> None:
    resp = _client().get("/register?code=AGR-001")
    assert resp.status_code == 200
    body = resp.text
    assert "Register a bin" in body
    assert 'value="AGR-001"' in body


def test_register_submit_exposes_pending_feedback() -> None:
    resp = _client().get("/register")

    assert resp.status_code == 200
    body = resp.text
    assert "data-submit-form" in body
    assert 'data-busy-label="Reading label…"' in body
    assert "data-submit-progress hidden" in body
    assert "Reading the printed label and placing this bin." in body


def test_register_confirmed_view_renders_done(monkeypatch) -> None:
    done = {
        "mode": "done",
        "bin_code": "AGR-001",
        "site": "alameda-garage",
        "already_existed": False,
        "has_gps": True,
        "lat": 37.7639,
        "lon": -122.2265,
        "accuracy_m": 11.7,
        "photo_stored": True,
    }
    monkeypatch.setattr(bin_photo_web, "_register_confirmed", lambda **_: done)
    resp = _client().post(
        "/register/confirm",
        data={"bin_code": "AGR-001", "site": "alameda-garage", "lat": "37.7639", "lon": "-122.23"},
        headers=_LOOPBACK_ORIGIN,
    )
    assert resp.status_code == 200
    body = resp.text
    assert "Bin registered" in body
    assert "AGR-001" in body
    assert "alameda-garage" in body


def test_register_confirm_requires_an_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def fake_confirm(**_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"mode": "done"}

    monkeypatch.setattr(bin_photo_web, "_register_confirmed", fake_confirm)

    resp = _client().post(
        "/register/confirm",
        data={
            "bin_code": "AGR-014",
            "site": "alameda-garage",
            "print_label": "1",
        },
    )

    assert resp.status_code == 403
    assert called is False


def test_register_confirm_rejects_cross_origin_print(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def fake_confirm(**_kwargs: object) -> dict[str, object]:
        nonlocal called
        called = True
        return {"mode": "done"}

    monkeypatch.setattr(bin_photo_web, "_register_confirmed", fake_confirm)

    resp = _client().post(
        "/register/confirm",
        data={
            "bin_code": "AGR-014",
            "site": "alameda-garage",
            "print_label": "1",
        },
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
    )

    assert resp.status_code == 403
    assert called is False


def test_confirm_route_carries_reviewed_theme_and_print_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(bin_register, "register_bin", lambda *_args, **_kwargs: _register_result())
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    submissions: list[str] = []

    def fake_send(data: str, **_kwargs: object) -> PrintPlan:
        submissions.append(data)
        return PrintPlan("cups", "OmezizyD450", len(data.encode("utf-8")))

    monkeypatch.setattr(bin_label, "send_to_printer", fake_send)

    resp = _client().post(
        "/register/confirm",
        data={
            "bin_code": "AGR-014",
            "site": "alameda-garage",
            "theme": "hand tools",
            "contents": "hex keys",
            "print_label": "1",
        },
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    assert len(submissions) == 1
    assert '"HAND TOOLS"' in submissions[0]
    assert '"hex keys"' in submissions[0]
    assert "1 label sent to OmezizyD450" in resp.text


def test_confirm_route_prints_two_reviewed_labels_in_one_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(bin_register, "register_bin", lambda *_args, **_kwargs: _register_result())
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    submissions: list[str] = []

    def fake_send(data: str, **_kwargs: object) -> PrintPlan:
        submissions.append(data)
        return PrintPlan("cups", "OmezizyD450", len(data.encode("utf-8")))

    monkeypatch.setattr(bin_label, "send_to_printer", fake_send)

    response = _client().post(
        "/register/confirm",
        data={
            "bin_code": "AGR-014",
            "site": "alameda-garage",
            "theme": "hand tools",
            "contents": "hex keys",
            "print_label": "1",
            "label_count": "2",
        },
        headers=_LOOPBACK_ORIGIN,
    )

    assert response.status_code == 200
    assert len(submissions) == 1
    assert "PRINT 1,2" in submissions[0]
    assert "2 labels sent to OmezizyD450" in response.text


def test_confirm_route_persists_the_reviewed_theme(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(conn))

    resp = _client().post(
        "/register/confirm",
        data={
            "bin_code": "AGR-015",
            "site": "alameda-garage",
            "theme": "  Network adapters  ",
            "contents": "USB-C and Ethernet adapters",
        },
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    row = conn.execute(
        """
        SELECT raw_payload->'metadata'->'bin_profile'
        FROM captures
        WHERE raw_payload->'metadata'->>'bin_code' = 'AGR-015'
        """
    ).fetchone()
    assert row == ({"theme": "Network adapters"},)


def test_explicit_print_renders_reviewed_label_and_submits_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(bin_register, "register_bin", lambda *_args, **_kwargs: _register_result())
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    submissions: list[tuple[str, str | None]] = []

    def fake_send(
        data: str,
        *,
        cups_queue: str | None = None,
        **_kwargs: object,
    ) -> PrintPlan:
        submissions.append((data, cups_queue))
        return PrintPlan("cups", cups_queue or "", len(data.encode("utf-8")))

    monkeypatch.setattr(bin_label, "send_to_printer", fake_send)

    view = bin_photo_web._register_and_view(
        bin_code="AGR-014",
        site="alameda-garage",
        gps=None,
        photo_sha="deadbeef",
        contents="hex keys",
        theme="hand tools",
        label_count=1,
    )

    assert view["mode"] == "done"
    assert view["printed"] is True
    assert len(submissions) == 1
    tspl, queue = submissions[0]
    assert queue == "OmezizyD450"
    assert '"HAND TOOLS"' in tspl
    assert '"AGR-014"' in tspl
    assert '"RETURN TO: ALA-GARAGE"' in tspl
    assert '"hex keys"' in tspl


def test_print_failure_does_not_hide_successful_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(bin_register, "register_bin", lambda *_args, **_kwargs: _register_result())
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")

    def fail_print(*_args: object, **_kwargs: object) -> None:
        raise bin_label.BinLabelError("printer is offline")

    monkeypatch.setattr(bin_label, "send_to_printer", fail_print)

    view = bin_photo_web._register_and_view(
        bin_code="AGR-014",
        site="alameda-garage",
        gps=None,
        photo_sha="deadbeef",
        contents="hex keys",
        theme="hand tools",
        label_count=1,
    )

    assert view["mode"] == "done"
    assert view["printed"] is False
    assert view["print_error"] == "printer is offline"


def test_replayed_registration_does_not_submit_a_second_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    already_existed = iter((False, True))
    monkeypatch.setattr(
        bin_register,
        "register_bin",
        lambda *_args, **_kwargs: _register_result(already_existed=next(already_existed)),
    )
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "OmezizyD450")
    submissions: list[str] = []

    def fake_send(data: str, **_kwargs: object) -> PrintPlan:
        submissions.append(data)
        return PrintPlan("cups", "OmezizyD450", len(data.encode("utf-8")))

    monkeypatch.setattr(bin_label, "send_to_printer", fake_send)
    kwargs = {
        "bin_code": "AGR-014",
        "site": "alameda-garage",
        "gps": None,
        "photo_sha": "deadbeef",
        "contents": "hex keys",
        "theme": "hand tools",
        "label_count": 1,
    }

    first = bin_photo_web._register_and_view(**kwargs)
    replay = bin_photo_web._register_and_view(**kwargs)

    assert first["printed"] is True
    assert replay["printed"] is False
    assert "duplicate label" in str(replay["print_error"])
    assert len(submissions) == 1


def test_registration_without_print_intent_never_submits_a_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_label, bin_register, db

    monkeypatch.setattr(db, "connect", lambda: nullcontext(object()))
    monkeypatch.setattr(bin_register, "register_bin", lambda *_args, **_kwargs: _register_result())

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("printer boundary must not be called")

    monkeypatch.setattr(bin_label, "send_to_printer", fail_if_called)

    view = bin_photo_web._register_and_view(
        bin_code="AGR-014",
        site="alameda-garage",
        gps=None,
        photo_sha="deadbeef",
        contents="hex keys",
        label_count=0,
    )

    assert view["mode"] == "done"
    assert view["print_requested"] is False
    assert view["printed"] is False


def test_register_post_is_origin_gated() -> None:
    resp = _client().post(
        "/register",
        files={"photo": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"bin_code": "AGR-001"},
        headers={"Origin": "http://evil.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert resp.status_code == 403


def test_ambiguous_site_confirmation_exposes_pending_feedback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pick_view = {
        "mode": "pick",
        "bin_code": "AGR-001",
        "code_source": "the label QR",
        "contents": "hex keys",
        "photo_sha256": "sha123",
        "lat": 37.7,
        "lon": -122.2,
        "accuracy_m": 10.0,
        "has_gps": True,
        "reason": "more than one site is nearby",
        "candidates": ["alameda-garage", "alameda-home"],
        "all_sites": ["alameda-garage", "alameda-home"],
    }
    monkeypatch.setattr(bin_photo_web, "_register_flow", lambda **_: pick_view)
    resp = _client().post(
        "/register",
        files={"photo": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    body = resp.text
    assert 'data-busy-label="Registering bin…"' in body
    assert "data-submit-progress hidden" in body
    assert "Registering this bin at the selected site." in body


def test_register_flow_registers_when_gps_resolves(monkeypatch) -> None:
    from binkeeper import bin_geo, bin_harvest

    monkeypatch.setattr(bin_photo_web, "_store_photos", lambda imgs: (["sha123"], None))
    monkeypatch.setattr(bin_geo, "extract_gps", lambda _img: bin_geo.GpsFix(37.7, -122.2, 10.0))
    monkeypatch.setattr(
        bin_harvest,
        "resolve_site",
        lambda *a, **k: bin_harvest.GeofenceResolution(
            site="alameda-garage", ambiguous=False, too_loose=False, candidates=("alameda-garage",)
        ),
    )
    captured = {}
    monkeypatch.setattr(
        bin_photo_web,
        "_register_and_view",
        lambda **kw: captured.update(kw) or {"mode": "done", "site": kw["site"]},
    )
    view = bin_photo_web._register_flow(image=b"x", bin_code="AGR-001", contents="stuff")
    assert view["mode"] == "done"
    assert captured["site"] == "alameda-garage"
    assert captured["photo_sha"] == "sha123"


def test_register_flow_asks_to_pick_when_gps_is_ambiguous(monkeypatch) -> None:
    from binkeeper import bin_geo, bin_harvest

    monkeypatch.setattr(bin_photo_web, "_store_photos", lambda imgs: (["sha123"], None))
    monkeypatch.setattr(bin_geo, "extract_gps", lambda _img: bin_geo.GpsFix(37.7, -122.2, 10.0))
    monkeypatch.setattr(
        bin_harvest,
        "resolve_site",
        lambda *a, **k: bin_harvest.GeofenceResolution(
            site=None,
            ambiguous=True,
            too_loose=False,
            candidates=("alameda-garage", "alameda-home"),
        ),
    )
    view = bin_photo_web._register_flow(image=b"x", bin_code="AGR-001", contents=None)
    assert view["mode"] == "pick"
    assert view["candidates"] == ["alameda-garage", "alameda-home"]
    assert view["photo_sha256"] == "sha123"
    assert "more than one" in str(view["reason"])


def test_detect_bin_code_entered_wins_and_normalizes() -> None:
    code, source = bin_photo_web._detect_bin_code(image=b"x", entered="agr-001")
    assert code == "AGR-001"
    assert source == "entered"


def test_detect_bin_code_reads_qr_when_blank(monkeypatch) -> None:
    from binkeeper import bin_geo

    monkeypatch.setattr(bin_geo, "decode_bin_code", lambda _img: "OFE-014")
    code, source = bin_photo_web._detect_bin_code(image=b"photo", entered=None)
    assert code == "OFE-014"
    assert source == "the label QR"


def test_detect_bin_code_falls_back_to_vision_ocr(monkeypatch) -> None:
    from binkeeper import bin_geo, bin_vision

    monkeypatch.setattr(bin_geo, "decode_bin_code", lambda _img: None)  # QR unreadable
    monkeypatch.setattr(bin_vision, "read_bin_code", lambda _client, _img: "AHM-003")
    code, source = bin_photo_web._detect_bin_code(image=b"photo", entered=None)
    assert code == "AHM-003"
    assert source == "the printed label"


def test_register_flow_prefers_device_geolocation_over_exif(monkeypatch) -> None:
    from binkeeper import bin_geo, bin_harvest

    monkeypatch.setattr(bin_photo_web, "_store_photos", lambda imgs: (["sha"], None))
    # EXIF would report a bogus fix; the device geolocation must win.
    monkeypatch.setattr(bin_geo, "extract_gps", lambda _img: bin_geo.GpsFix(0.0, 0.0, 5.0))
    seen = {}

    def fake_resolve(lat, lon, sites, **k):
        seen["lat"], seen["lon"] = lat, lon
        return bin_harvest.GeofenceResolution(
            site="alameda-garage", ambiguous=False, too_loose=False, candidates=("alameda-garage",)
        )

    monkeypatch.setattr(bin_harvest, "resolve_site", fake_resolve)
    monkeypatch.setattr(
        bin_photo_web,
        "_register_and_view",
        lambda **kw: {"mode": "done", "used_lat": getattr(kw["gps"], "lat", None)},
    )
    view = bin_photo_web._register_flow(
        image=b"x",
        bin_code="AGR-001",
        contents=None,
        geo_lat=37.7,
        geo_lon=-122.2,
        geo_accuracy=8.0,
    )
    assert seen["lat"] == 37.7  # device geolocation, not the EXIF 0.0
    assert view["used_lat"] == 37.7


def test_register_flow_asks_when_no_code_can_be_read(monkeypatch) -> None:
    from binkeeper import bin_geo, bin_vision

    monkeypatch.setattr(bin_geo, "decode_bin_code", lambda _img: None)
    monkeypatch.setattr(bin_vision, "read_bin_code", lambda _client, _img: None)
    view = bin_photo_web._register_flow(image=b"x", bin_code=None, contents=None)
    assert view["mode"] == "form"
    assert "couldn't read a bin code" in str(view["error"])


def _triage_view(*, bin_code: str, tenant_id: str, corpus_id: str) -> bin_manage_web.ManageView:
    return {
        "bin_code": bin_code,
        "theme": "Power tools",
        "contents": "",
        "home_site": "alameda-garage",
        "current_site": "oakland-fab-east",
        "catalog_photo_url": None,
    }


def test_manage_not_found_records_the_miss_and_opens_triage() -> None:
    recorded: list[tuple[str, str, str]] = []

    def record(action: bin_manage_web.RetrievalAction, scope: object) -> None:
        recorded.append((action.outcome, action.bin_code, action.site))

    def load_triage(
        *, bin_code: str, site: str, tenant_id: str, corpus_id: str
    ) -> list[bin_manage_web.TriageCandidate]:
        assert (bin_code, site) == ("AGR-014", "oakland-fab-east")
        return [{"bin_code": "OFE-002", "confidence_label": "41%", "stale": True}]

    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        manage_loader=_triage_view,
        triage_loader=load_triage,
        retrieval_recorder=record,
    )
    with TestClient(app) as client:
        response = client.post(
            "/manage/AGR-014/not-found",
            data={"action_id": "nf-1", "site": "oakland-fab-east"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/manage/AGR-014/triage?notice=not-found-recorded"
        assert recorded == [("not_found", "AGR-014", "oakland-fab-east")]

        page = client.get("/manage/AGR-014/triage", params={"notice": "not-found-recorded"})

    assert page.status_code == 200
    body = page.text
    assert "Where else at Oakland Fab East?" in body
    assert "OFE-002" in body
    assert "Seen it" in body
    assert "Not here either" in body
    assert "Found it after all" in body
    assert "mark each bin you actually see" in body.lower()


def test_triage_marks_emit_exactly_one_event_each() -> None:
    recorded: list[tuple[str, str, str]] = []

    def record(action: bin_manage_web.RetrievalAction, scope: object) -> None:
        recorded.append((action.outcome, action.bin_code, action.site))

    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        manage_loader=_triage_view,
        triage_loader=lambda **_: [],
        retrieval_recorder=record,
    )
    common = {"site": "oakland-fab-east", "action_id": "mk-1"}
    with TestClient(app) as client:
        seen = client.post(
            "/manage/AGR-014/triage/mark",
            data={**common, "target_bin": "OFE-002", "verdict": "seen"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        missing = client.post(
            "/manage/AGR-014/triage/mark",
            data={**common, "target_bin": "OFE-003", "verdict": "missing"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        found = client.post(
            "/manage/AGR-014/triage/mark",
            data={**common, "verdict": "found"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert recorded == [
        ("confirm", "OFE-002", "oakland-fab-east"),
        ("not_found", "OFE-003", "oakland-fab-east"),
        ("fetch", "AGR-014", "oakland-fab-east"),
    ]
    assert seen.headers["location"].endswith("/triage?notice=candidate-confirmed")
    assert missing.headers["location"].endswith("/triage?notice=candidate-missing")
    assert found.headers["location"] == "/manage/AGR-014?notice=found-recorded"


def test_retrieval_outcome_round_trips_to_the_trip_ledger(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db
    from binkeeper.bin_inventory import bin_belief
    from binkeeper.bin_register import register_bin

    register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        observed_at=datetime(2026, 7, 13, 10, tzinfo=UTC),
    )
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    before = bin_belief(conn, "AGR-014").confidence

    with _client() as client:
        response = client.post(
            "/manage/AGR-014/not-found",
            data={"action_id": "nf-db-1", "site": "alameda-garage"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert response.status_code == 303
    after = bin_belief(conn, "AGR-014")
    assert after.confidence < before
    row = conn.execute(
        "SELECT event_kind, site, source_label FROM bin_trip_events "
        "WHERE bin_code = 'AGR-014' AND event_kind = 'not_found'"
    ).fetchone()
    assert row == ("not_found", "alameda-garage", "manage-web")


def _deck_view() -> dict[str, object]:
    return {
        "stash_run_id": "run-1",
        "site": "alameda-garage",
        "cards": [
            {
                "routing_request_id": "req-deck",
                "text": "usb cable",
                "disposition": "deck",
                "recommended_bin_code": "AGR-001",
                "recommended_score_label": "72%",
                "alternatives": [{"bin_code": "AGR-002", "score_label": "55%"}],
                "abstain_flags": [],
            },
            {
                "routing_request_id": "req-pending",
                "text": "mystery gadget",
                "disposition": "pending",
                "recommended_bin_code": None,
                "recommended_score_label": "—",
                "alternatives": [{"bin_code": "AGR-003", "score_label": "41%"}],
                "abstain_flags": ["top_score_below_floor"],
            },
        ],
        "decided": [
            {"text": "zip ties", "decision_kind": "accept", "selected_bin_code": "AGR-002"}
        ],
    }


def test_stash_deck_deals_cards_and_pending_pile() -> None:
    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        stash_deck_loader=lambda **_: _deck_view(),
    )
    response = TestClient(app).get("/stash/run-1")

    assert response.status_code == 200
    body = response.text
    assert "usb cable" in body
    assert "Put it in AGR-001" in body
    assert "Instead: AGR-002 (55%)" in body
    assert "Not an item" in body
    assert "None of these" in body
    assert "mystery gadget" in body
    assert "top_score_below_floor" in body
    assert "Put it in AGR-003" not in body  # pending cards never offer accept
    assert "zip ties — accept" in body.replace("\n", " ") or "accept" in body
    assert "1 card to deal" in body


def test_stash_is_reachable_from_the_tab_bar_and_marks_itself_current() -> None:
    client = TestClient(
        bin_photo_web.create_app(
            host="127.0.0.1",
            port=8765,
            stash_deck_loader=lambda **_: _deck_view(),
        )
    )

    photo_page = client.get("/")
    assert photo_page.status_code == 200
    assert 'data-binkeeper-section="stash"' in photo_page.text
    assert 'href="/stash"' in photo_page.text

    deck_page = client.get("/stash/run-1")
    assert deck_page.status_code == 200
    assert 'data-binkeeper-section="stash" aria-current="page"' in deck_page.text


def test_stash_decide_taps_map_one_to_one_onto_decision_kinds() -> None:
    recorded: list[tuple[str, str, str | None]] = []

    def record(decision: bin_stash_web.StashDecision, scope: object) -> None:
        recorded.append(
            (decision.decision_kind, decision.routing_request_id, decision.selected_bin_code)
        )

    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        stash_deck_loader=lambda **_: _deck_view(),
        stash_decision_recorder=record,
    )
    common = {"routing_request_id": "req-deck", "action_id": "d-1"}
    with TestClient(app) as client:
        for decision, selected in (
            ("accept", None),
            ("override", "AGR-002"),
            ("reject", None),
            ("not_an_item", None),
        ):
            data = {**common, "decision": decision}
            if selected:
                data["selected_bin"] = selected
            response = client.post(
                "/stash/run-1/decide",
                data=data,
                headers=_LOOPBACK_ORIGIN,
                follow_redirects=False,
            )
            assert response.status_code == 303
            assert f"notice=decided-{decision}" in response.headers["location"]
        bogus = client.post(
            "/stash/run-1/decide",
            data={**common, "decision": "split"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert recorded == [
        ("accept", "req-deck", None),
        ("override", "req-deck", "AGR-002"),
        ("reject", "req-deck", None),
        ("not_an_item", "req-deck", None),
    ]
    assert "notice=decide-invalid" in bogus.headers["location"]


def test_stash_run_round_trips_receipts_and_decisions(
    conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import db

    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    with _client() as client:
        created = client.post(
            "/stash",
            data={"site": "alameda-garage", "items": "usb cable\nzip ties", "action_id": "r-1"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        assert created.status_code == 303
        run_path = created.headers["location"]
        deck = client.get(run_path)
        assert deck.status_code == 200
        assert "usb cable" in deck.text

        request_id = conn.execute(
            "SELECT id::text FROM bin_routing_requests WHERE input_text = 'usb cable'"
        ).fetchone()[0]
        decided = client.post(
            f"{run_path}/decide",
            data={
                "routing_request_id": request_id,
                "decision": "not_an_item",
                "action_id": "d-db-1",
            },
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )
        assert decided.status_code == 303
        after = client.get(run_path)

    row = conn.execute(
        "SELECT decision_kind FROM bin_placement_decisions WHERE routing_request_id = %s",
        (request_id,),
    ).fetchone()
    assert row == ("not_an_item",)
    assert "usb cable — not an item" in after.text.replace("</code>", "")


def test_wave_page_lists_stops_and_completion_taps(monkeypatch: pytest.MonkeyPatch) -> None:
    completions: list[tuple[str, str]] = []

    def load_wave(*, stash_run_id: str, tenant_id: str, corpus_id: str) -> dict[str, object]:
        return {
            "stash_run_id": stash_run_id,
            "site": "alameda-garage",
            "completed_count": 1,
            "stops": [
                {
                    "bin_code": "AGR-001",
                    "capacity_state": "full",
                    "item_texts": ["zip ties"],
                    "completed": False,
                },
                {
                    "bin_code": "AGR-002",
                    "capacity_state": "half",
                    "item_texts": ["usb cable"],
                    "completed": True,
                },
            ],
        }

    def complete(stash_run_id: str, bin_code: str, scope: object) -> None:
        completions.append((stash_run_id, bin_code))

    app = bin_photo_web.create_app(
        host="127.0.0.1",
        port=8765,
        wave_plan_loader=load_wave,
        wave_stop_completer=complete,
    )
    with TestClient(app) as client:
        page = client.get("/stash/run-9/wave")
        done = client.post(
            "/stash/run-9/wave/complete",
            data={"bin_code": "AGR-001"},
            headers=_LOOPBACK_ORIGIN,
            follow_redirects=False,
        )

    assert page.status_code == 200
    body = page.text
    assert body.count("Placed — record it") == 1  # only the incomplete stop offers the tap
    assert "consider a decant or split" in body  # the full bin is flagged
    assert "Done — recorded into the bin" in body
    assert done.status_code == 303
    assert "notice=stop-done" in done.headers["location"]
    assert completions == [("run-9", "AGR-001")]


def test_photo_drop_survives_a_vision_timeout(monkeypatch) -> None:
    # Regression 2026-07-29: a cold model load on the vision host let a raw
    # TimeoutError escape the advisory lane, so the drop page 500'd and the
    # owner lost the form -- while the photos had in fact already been stored.
    # Vision is advisory (README invariant); a slow model degrades to a message.
    def fake_urlopen(request, *, timeout):
        raise TimeoutError("timed out")

    monkeypatch.setattr("binkeeper.bin_vision.urllib.request.urlopen", fake_urlopen)
    # A synthetic key keeps the ADR 0005 default (openrouter) provider on the
    # transport path so the faked timeout, not a missing key, is what degrades.
    monkeypatch.setenv("BINKEEPER_OPENROUTER_API_KEY", "synthetic-key")
    resp = _client().post(
        "/",
        files={"photos": ("bin.jpg", _ONE_PIXEL_JPEG, "image/jpeg")},
        data={"notes": "", "site": "alameda-garage"},
        headers=_LOOPBACK_ORIGIN,
    )

    assert resp.status_code == 200
    assert "did not respond within" in resp.text
