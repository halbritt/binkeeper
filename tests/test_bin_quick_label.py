"""Fast label creation without the advisory vision lane."""

from __future__ import annotations

from contextlib import nullcontext

import psycopg
import pytest
from fastapi.testclient import TestClient

from binkeeper import bin_label, bin_photo_web, db
from binkeeper.bin_inventory import bin_where
from binkeeper.bin_label import BinLabelError, PrintPlan
from binkeeper.bin_passport import bin_passport
from binkeeper.bin_quick_label import QuickLabelError, QuickLabelRequest, create_quick_label

_ORIGIN = {"Origin": "http://testserver:8765", "Sec-Fetch-Site": "same-origin"}
_ACTION = "b9859603-cd13-4d0c-8abe-c8cbda009da4"


def _request(**changes: object) -> QuickLabelRequest:
    values: dict[str, object] = {
        "site": "alameda-garage",
        "theme": "Spare fasteners",
        "contents": "",
        "printer": "cups",
        "copies": 1,
        "action_id": _ACTION,
    }
    values.update(changes)
    return QuickLabelRequest(**values)  # type: ignore[arg-type]


def test_quick_label_registers_without_a_photo_or_vision_and_replay_does_not_reprint(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from binkeeper import bin_quick_label

    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "synthetic-queue")
    sent: list[bytes] = []

    def send(job: bin_label.LabelJob) -> PrintPlan:
        sent.append(job.payload)
        return PrintPlan("cups", "synthetic-queue", len(job.payload))

    monkeypatch.setattr(bin_quick_label, "send_label_job", send)

    first = create_quick_label(_request())
    replay = create_quick_label(_request())

    assert first.bin_code == "AGR-001"
    assert first.printed
    assert replay.already_existed and not replay.printed
    assert len(sent) == 1
    assert bin_passport(conn, first.bin_code).theme == "Spare fasteners"
    assert bin_where(conn, first.bin_code).site == "alameda-garage"
    metadata = conn.execute(
        "SELECT raw_payload->'metadata' FROM captures WHERE raw_payload->>'source_label' = %s",
        (f"quick-label:{_ACTION}",),
    ).fetchone()[0]
    assert "photo" not in metadata
    assert metadata["quick_label"] == {"action_id": _ACTION, "printer": "cups", "copies": 1}

    with pytest.raises(QuickLabelError, match="already used"):
        create_quick_label(_request(theme="Different theme"))
    assert len(sent) == 1


def test_quick_label_assigns_distinct_codes_and_keeps_failed_print_registration(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from binkeeper import bin_quick_label

    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(bin_label, "BIN_LABEL_CUPS_QUEUE", "synthetic-queue")

    def printer_down(_job: object) -> PrintPlan:
        raise BinLabelError("synthetic printer unavailable")

    monkeypatch.setattr(bin_quick_label, "send_label_job", printer_down)
    first = create_quick_label(_request())
    second = create_quick_label(_request(action_id="e0d2317a-5e50-4c65-9d9d-cc655d296263"))

    assert (first.bin_code, second.bin_code) == ("AGR-001", "AGR-002")
    assert not first.printed
    assert first.print_error == "synthetic printer unavailable"
    assert bin_passport(conn, second.bin_code).theme == "Spare fasteners"


def test_quick_label_reports_uncertain_b1_completion_without_reprinting(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from binkeeper import bin_quick_label
    from binkeeper.bin_b1 import B1PrintUnknown

    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    monkeypatch.setattr(bin_quick_label, "make_label_job", lambda *_args, **_kwargs: object())
    calls = 0

    def uncertain(_job: object) -> PrintPlan:
        nonlocal calls
        calls += 1
        raise B1PrintUnknown("synthetic completion unknown")

    monkeypatch.setattr(bin_quick_label, "send_label_job", uncertain)
    first = create_quick_label(_request(printer="niimbot-b1"))
    replay = create_quick_label(_request(printer="niimbot-b1"))

    assert first.print_unknown
    assert not first.printed
    assert replay.already_existed
    assert calls == 1


def test_quick_label_rejects_missing_theme_and_unknown_site_without_writing(
    conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "connect", lambda **_kwargs: nullcontext(conn))
    with pytest.raises(QuickLabelError, match="theme"):
        create_quick_label(_request(theme=""))
    with pytest.raises(QuickLabelError, match="site"):
        create_quick_label(_request(site="unknown-site"))
    assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0


def test_quick_label_page_and_post_require_explicit_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from binkeeper import bin_b1, bin_quick_label

    calls: list[QuickLabelRequest] = []

    def fake_create(request: QuickLabelRequest, **_kwargs: str):
        calls.append(request)
        return bin_quick_label.QuickLabelResult("AGR-001", request.site, False, True, "cups", None)

    monkeypatch.setattr(bin_photo_web, "create_quick_label", fake_create)
    monkeypatch.setattr(bin_b1, "B1_ADDRESS", "synthetic-address")
    client = TestClient(bin_photo_web.create_app(host="127.0.0.1", port=8765))
    form = client.get("/quick-label")
    assert form.status_code == 200
    assert "Print a label now" in form.text
    assert 'name="theme"' in form.text
    assert 'name="site"' in form.text
    assert 'name="action_id"' in form.text
    assert 'name="photos"' not in form.text
    assert '<option value="cups" selected>' in form.text
    assert '<option value="niimbot-b1"' in form.text
    payload = {
        "action_id": _ACTION,
        "theme": "Spare fasteners",
        "site": "alameda-garage",
        "printer": "cups",
        "label_count": "1",
    }
    denied = client.post("/quick-label", data=payload)
    accepted = client.post("/quick-label", data=payload, headers=_ORIGIN)
    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert "Contents photo still needed" in accepted.text
    assert 'href="/manage/AGR-001#photos"' in accepted.text
    assert len(calls) == 1
