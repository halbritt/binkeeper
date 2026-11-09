"""New-bin intent must remain distinct from observing an existing physical label."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from binkeeper import bin_photo_web, db
from binkeeper.bin_create import BinCreationConflict, CreateBinRequest, create_bin
from binkeeper.bin_inventory import bin_where, record_event
from binkeeper.bin_passport import bin_passport
from binkeeper.blob_vault import InMemoryBlobStore, put_blob

ORIGIN = {"Origin": "http://testserver:8765"}
START = datetime(2026, 11, 8, 23, 58, tzinfo=UTC)


def _request(**changes):
    return replace(
        CreateBinRequest(str(uuid4()), "AGR-123", "alameda-garage", "My tools", "My calipers"),
        **changes,
    )


def _photos(conn, count):
    store = InMemoryBlobStore()
    return tuple(
        put_blob(
            conn,
            store,
            f"synthetic photo {i}".encode(),
            key=b"s" * 32,
            key_ref="synthetic",
            content_type="image",
        ).plaintext_sha256
        for i in range(count)
    )


@pytest.mark.parametrize("count", [0, 1, 3])
def test_distinct_creation_intents_cannot_silently_share_a_bin(conn, monkeypatch, count):
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    client = TestClient(bin_photo_web.create_app())
    fields = {
        "operation": "create",
        "bin_code": "AGR-123",
        "site": "alameda-garage",
        "theme": "First bin",
        "contents": "First contents",
        "print_label": "0",
    }
    photos = _photos(conn, count * 2)
    first_photos = (
        {"photo_sha256": photos[0], "additional_photo_sha256": photos[1:count]} if count else {}
    )
    second_photos = (
        {"photo_sha256": photos[count], "additional_photo_sha256": photos[count + 1 :]}
        if count
        else {}
    )
    first = client.post(
        "/register/confirm",
        headers=ORIGIN,
        data={**fields, **first_photos, "action_id": str(uuid4())},
    )
    second = client.post(
        "/register/confirm",
        headers=ORIGIN,
        data={**fields, **second_photos, "action_id": str(uuid4()), "theme": "Second bin"},
    )

    assert first.status_code == 200 and "Bin registered" in first.text
    assert second.status_code == 409
    assert "already in use" in second.text
    assert 'value="Second bin"' in second.text
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (count + 1,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (1,)
    linked = conn.execute(
        "SELECT raw_payload->'metadata'->'photo'->>'blob_ref' FROM captures "
        "WHERE raw_payload->'metadata' ? 'photo'"
    ).fetchall()
    assert {row[0] for row in linked} == set(photos[:count])


@pytest.mark.parametrize("count", [1, 3])
def test_creation_identity_survives_midnight_and_an_intervening_move(conn, count):
    request = _request(photo_refs=_photos(conn, count))
    first = create_bin(conn, request, observed_at=START)
    record_event(
        conn,
        event_kind="place",
        bin_code=first.bin_code,
        site="oakland-fab-east",
        occurred_at=START + timedelta(minutes=1),
        idempotency_key="intervening-move",
    )
    replay = create_bin(conn, request, observed_at=START + timedelta(days=1))

    assert replay.already_existed and replay.capture_id == first.capture_id
    assert bin_where(conn, first.bin_code).site == "oakland-fab-east"
    passport = bin_passport(conn, first.bin_code)
    assert passport.theme == "My tools" and "My calipers" in passport.sibling_contents
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (count + 1,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (2,)
    linked = conn.execute("""SELECT raw_payload->'metadata'->'photo'->>'blob_ref'
        FROM captures WHERE raw_payload->'metadata' ? 'photo'""").fetchall()
    assert {row[0] for row in linked} == set(request.photo_refs)


@pytest.mark.parametrize(
    "change",
    [
        {"theme": "Different"},
        {"contents": "Different"},
        {"site": "oakland-fab-east"},
        {"copies": 2},
        {"printer": "niimbot-b1"},
        {"bin_code": "AGR-124"},
    ],
)
def test_action_reuse_with_different_reviewed_details_conflicts(conn, change):
    request = _request()
    create_bin(conn, request)
    with pytest.raises(BinCreationConflict, match="different details"):
        create_bin(conn, replace(request, **change))
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (1,)


def test_photo_order_is_part_of_creation_identity(conn):
    request = _request(photo_refs=_photos(conn, 2))
    create_bin(conn, request)
    with pytest.raises(BinCreationConflict):
        create_bin(conn, replace(request, photo_refs=request.photo_refs[::-1]))


@pytest.mark.parametrize("same_action", [False, True])
def test_concurrent_creation_allocates_once_per_action(conn, same_action):
    barrier = Barrier(4)
    request = _request(bin_code="")
    requests = [
        request if same_action else replace(request, action_id=str(uuid4())) for _ in range(4)
    ]

    def run(request):
        with psycopg.connect(conn.info.dsn, autocommit=True) as worker:
            barrier.wait(timeout=5)
            return create_bin(worker, request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, requests))
    assert {result.bin_code for result in results} == (
        {"AGR-001"} if same_action else {"AGR-001", "AGR-002", "AGR-003", "AGR-004"}
    )
    assert sum(not result.already_existed for result in results) == (1 if same_action else 4)


def test_failure_after_photo_linking_rolls_back_the_entire_creation(conn, monkeypatch):
    from binkeeper import bin_create

    request = _request(photo_refs=_photos(conn, 2))
    append = bin_create.append_saved_bin_photos

    def interrupted(*args, **kwargs):
        append(*args, **kwargs)
        raise OSError("synthetic failure after links")

    monkeypatch.setattr(bin_create, "append_saved_bin_photos", interrupted)
    with pytest.raises(OSError, match="after links"):
        create_bin(conn, request)
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM evidence_blobs").fetchone() == (2,)
    monkeypatch.setattr(bin_create, "append_saved_bin_photos", append)
    assert not create_bin(conn, request).already_existed


def test_replayed_confirmation_never_submits_a_second_print(conn, monkeypatch):
    from binkeeper import bin_create, bin_label

    clock = START

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    sent = []
    monkeypatch.setattr(bin_create, "datetime", Clock)
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "synthetic-queue")
    monkeypatch.setattr(
        bin_label,
        "send_label_job",
        lambda job: (
            sent.append(job) or bin_label.PrintPlan("cups", "synthetic-queue", len(job.payload))
        ),
    )
    fields = {
        "operation": "create",
        "action_id": str(uuid4()),
        "bin_code": "AGR-123",
        "site": "alameda-garage",
        "theme": "My tools",
        "contents": "My calipers",
        "print_label": "1",
        "label_count": "2",
    }
    client = TestClient(bin_photo_web.create_app())
    assert client.post("/register/confirm", headers=ORIGIN, data=fields).status_code == 200
    record_event(
        conn,
        event_kind="place",
        bin_code="AGR-123",
        site="oakland-fab-east",
        occurred_at=START + timedelta(minutes=1),
        idempotency_key="move",
    )
    clock += timedelta(days=1)
    response = client.post("/register/confirm", headers=ORIGIN, data=fields)
    assert response.status_code == 200 and "No second label was sent" in response.text
    assert "Open the bin page for its current location" in response.text
    assert "AGR-123</strong> is placed at" not in response.text
    assert len(sent) == 1
    assert bin_where(conn, "AGR-123").site == "oakland-fab-east"


@pytest.mark.parametrize(
    "changes", [{"action_id": ""}, {"operation": ""}, {"action_id": "invalid"}]
)
def test_stale_creation_form_preserves_review_without_writing(conn, monkeypatch, changes):
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    fields = {
        "operation": "create",
        "action_id": str(uuid4()),
        "bin_code": "AGR-123",
        "site": "alameda-garage",
        "theme": "My tools",
        "contents": "My calipers",
        "photo_sha256": "a" * 64,
        "additional_photo_sha256": ["b" * 64, "c" * 64],
        "print_label": "1",
        "label_count": "2",
        "printer": "niimbot-b1",
        **changes,
    }
    response = TestClient(bin_photo_web.create_app()).post(
        "/register/confirm", headers=ORIGIN, data=fields
    )
    assert response.status_code == 409
    assert "Review the saved details" in response.text
    assert 'value="My tools"' in response.text and ">My calipers</textarea>" in response.text
    assert 'value="AGR-123"' in response.text and 'value="alameda-garage" selected' in response.text
    assert 'name="label_count" value="2" checked' in response.text
    assert 'value="niimbot-b1" selected' in response.text
    assert all(digest * 64 in response.text for digest in "abc")
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (0,)


def test_failed_write_retains_action_identity_and_all_reviewed_context(conn, monkeypatch):
    def unavailable(*args, **kwargs):
        raise psycopg.OperationalError("private synthetic failure")

    monkeypatch.setattr(db, "connect", unavailable)
    action = str(uuid4())
    fields = {
        "operation": "create",
        "action_id": action,
        "bin_code": "AGR-123",
        "site": "alameda-garage",
        "theme": "My tools",
        "contents": "My calipers",
        "photo_sha256": "a" * 64,
        "additional_photo_sha256": ["b" * 64, "c" * 64],
        "print_label": "1",
        "label_count": "2",
        "printer": "niimbot-b1",
    }
    response = TestClient(bin_photo_web.create_app()).post(
        "/register/confirm", headers=ORIGIN, data=fields
    )
    assert response.status_code == 503
    assert f'name="action_id" value="{action}"' in response.text
    assert 'value="My tools"' in response.text and ">My calipers</textarea>" in response.text
    assert 'value="AGR-123"' in response.text and 'value="alameda-garage" selected' in response.text
    assert 'name="label_count" value="2" checked' in response.text
    assert 'value="niimbot-b1" selected' in response.text
    assert all(digest * 64 in response.text for digest in "abc")
    assert "private synthetic failure" not in response.text


def test_creation_through_paired_front_preserves_identity_and_requires_new_review(
    conn, monkeypatch
):
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    host = "synthetic.tail.test"
    client = TestClient(
        bin_photo_web.create_app(port=8766, allowed_paired_origins=(("https", host),)),
        base_url=f"https://{host}:8766",
    )
    headers = {"Origin": f"https://{host}:8766", "X-Forwarded-Proto": "https"}
    fields = {
        "operation": "create",
        "action_id": str(uuid4()),
        "bin_code": "",
        "site": "alameda-garage",
        "theme": "First bin",
        "contents": "First contents",
        "print_label": "0",
    }
    first = client.post("/register/confirm", headers=headers, data=fields)
    assert first.status_code == 200 and "AGR-001" in first.text
    fields["theme"] = "Second bin"
    conflict = client.post("/register/confirm", headers=headers, data=fields)
    assert conflict.status_code == 409 and "Review as a new bin" in conflict.text
    review = client.post("/register/confirm", headers=headers, data={**fields, "new_action": "1"})
    assert review.status_code == 200 and "Review bin details" in review.text
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (1,)

    class ActionParser(HTMLParser):
        action = ""

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "input" and attributes.get("name") == "action_id":
                self.action = attributes["value"]

    parser = ActionParser()
    parser.feed(review.text)
    assert parser.action and parser.action != fields["action_id"]
    second = client.post(
        "/register/confirm", headers=headers, data={**fields, "action_id": parser.action}
    )
    assert second.status_code == 200 and "AGR-002" in second.text
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (2,)


def test_physical_observation_keeps_day_scoped_registration_without_printing(conn, monkeypatch):
    from binkeeper import bin_label

    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(
        bin_label, "send_label_job", lambda job: pytest.fail("Observation must not print")
    )
    client = TestClient(bin_photo_web.create_app())
    fields = {"bin_code": "AGR-123", "site": "alameda-garage", "contents": "Original contents"}
    stale = client.post("/register/confirm", headers=ORIGIN, data=fields)
    assert stale.status_code == 409 and 'name="operation" value="observe"' in stale.text
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (0,)
    for _ in range(2):
        response = client.post(
            "/register/confirm",
            headers=ORIGIN,
            data={**fields, "operation": "observe", "print_label": "1"},
        )
        assert response.status_code == 200
    assert conn.execute("SELECT count(*) FROM captures").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM bin_trip_events").fetchone() == (1,)
