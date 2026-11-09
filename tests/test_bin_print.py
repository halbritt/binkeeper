"""Every physical label attempt must have durable, honest accounting."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from datetime import UTC, datetime
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest

from binkeeper import bin_label, db
from binkeeper.bin_register import register_bin


def test_print_reservation_precedes_transport_and_outcome_survives_replay(conn, monkeypatch):
    from binkeeper.bin_print import print_history, submit_label

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    calls = []

    def send(job):
        history = print_history(conn, "AGR-123")
        assert len(history) == 1 and history[0].status == "unresolved"
        assert history[0].copies == 2
        calls.append(job)
        return bin_label.PrintPlan("cups", "synthetic", len(job.payload))

    monkeypatch.setattr(bin_label, "send_label_job", send)
    job = bin_label.LabelJob("cups:synthetic", "tspl", b"synthetic print", 2)
    action = str(uuid4())
    first = submit_label("AGR-123", action, job)
    replay = submit_label("AGR-123", action, job)

    assert first.status == "submitted" and not first.replayed
    assert replay.replayed and replay.status == "submitted"
    assert len(calls) == 1
    assert print_history(conn, "AGR-123")[0].status == "submitted"


@pytest.mark.parametrize(
    "failure,expected", [("rejected", "failed"), ("timeout", "unknown"), ("b1", "unknown")]
)
def test_observed_failures_are_durable_and_do_not_retry(conn, monkeypatch, failure, expected):
    from binkeeper.bin_b1 import B1PrintUnknown
    from binkeeper.bin_print import print_history, submit_label

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    calls = []

    def fail(job):
        calls.append(job)
        if failure == "b1":
            raise B1PrintUnknown("synthetic completion unknown")
        if failure == "timeout":
            raise bin_label.BinLabelError("synthetic timeout") from subprocess.TimeoutExpired(
                "fake-printer", 1
            )
        raise bin_label.BinLabelError("synthetic rejection")

    monkeypatch.setattr(bin_label, "send_label_job", fail)
    job = bin_label.LabelJob("cups:synthetic", "tspl", b"synthetic print", 1)
    action = str(uuid4())
    result = submit_label("AGR-123", action, job)
    assert result.status == expected
    assert submit_label("AGR-123", action, job).replayed
    assert print_history(conn, "AGR-123")[0].status == expected
    assert len(calls) == 1


@pytest.mark.parametrize("version", [1, 2])
def test_legacy_intent_suppresses_replay_even_when_backend_changes(conn, monkeypatch, version):
    from binkeeper.bin_print import print_history, submit_label
    from binkeeper.personal_memory import CaptureRequest, PersonalMemoryService

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    action = str(uuid4())
    metadata = {
        "kind": "bin_label_print_intent",
        "schema_version": f"bin_label_print_intent.v{version}",
        "bin_code": "AGR-123",
        "intent_id": action,
        "target": "cups:old-queue",
        "payload_format": "tspl",
        "payload_sha256": "a" * 64,
    }
    PersonalMemoryService(conn).capture(
        CaptureRequest(
            "Legacy print request",
            "observation",
            1,
            datetime.now(UTC),
            "binkeeper-manage",
            f"binprint:AGR-123:{action}",
            metadata,
        )
    )
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    monkeypatch.setattr(
        bin_label, "send_label_job", lambda job: pytest.fail("Legacy action reprinted")
    )
    result = submit_label(
        "AGR-123", action, bin_label.LabelJob("niimbot-b1:fake", "png", b"new", 2)
    )
    assert result.replayed and result.status == "unresolved"
    assert result.target == "cups:old-queue" and result.copies is None
    assert print_history(conn, "AGR-123")[0].status == "unresolved"
    assert conn.execute("SELECT count(*) FROM capture_evidence").fetchone() == (2,)


@pytest.mark.parametrize(
    "point,attempts,status",
    [
        ("before_transport", 0, "unresolved"),
        ("after_transport", 1, "unresolved"),
        ("before_response", 1, "submitted"),
    ],
)
def test_process_death_and_restart_never_reclaim_an_intent(conn, tmp_path, point, attempts, status):
    from binkeeper.bin_print import print_history

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    action = str(uuid4())
    marker = tmp_path / "physical-attempts"
    script = """
import json, os, sys
from pathlib import Path
from binkeeper import bin_label
from binkeeper.bin_print import submit_label
point, action, marker = sys.argv[1:]
def fake_printer(job):
    if point == 'before_transport': os._exit(23)
    with Path(marker).open('a') as out: out.write('attempt\\n')
    if point == 'after_transport': os._exit(23)
    return bin_label.PrintPlan('cups', 'synthetic', len(job.payload))
bin_label.send_label_job = fake_printer
job = bin_label.LabelJob('cups:synthetic', 'tspl', b'synthetic', 1)
result = submit_label('AGR-123', action, job)
if point == 'before_response': os._exit(23)
print(json.dumps({'status': result.status, 'replayed': result.replayed}))
"""
    env = {**os.environ, "BINKEEPER_DATABASE_URL": conn.info.dsn}
    first = subprocess.run(
        [sys.executable, "-c", script, point, action, str(marker)],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert first.returncode == 23, first.stderr.decode()
    replay = subprocess.run(
        [sys.executable, "-c", script, "replay", action, str(marker)],
        env=env,
        capture_output=True,
        timeout=15,
    )
    assert replay.returncode == 0, replay.stderr.decode()
    assert json.loads(replay.stdout) == {"status": status, "replayed": True}
    assert (len(marker.read_text().splitlines()) if marker.exists() else 0) == attempts
    assert print_history(conn, "AGR-123")[0].status == status


def test_concurrent_print_requests_commit_one_reservation(conn, monkeypatch):
    from binkeeper.bin_print import submit_label

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    monkeypatch.setattr(db, "connect", lambda **_: psycopg.connect(conn.info.dsn))
    calls = []
    monkeypatch.setattr(
        bin_label,
        "send_label_job",
        lambda job: calls.append(job) or bin_label.PrintPlan("cups", "synthetic", 1),
    )
    barrier = Barrier(4)
    action = str(uuid4())

    def run(_):
        barrier.wait(timeout=5)
        return submit_label(
            "AGR-123", action, bin_label.LabelJob("cups:synthetic", "tspl", b"fake", 1)
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(4)))
    assert len(calls) == 1
    assert sum(not result.replayed for result in results) == 1


def test_lost_outcome_write_leaves_visible_unresolved_attempt(conn, monkeypatch):
    from fastapi.testclient import TestClient

    from binkeeper import bin_photo_web, bin_print

    register_bin(conn, bin_code="AGR-123", site="alameda-garage")
    monkeypatch.setattr(db, "connect", lambda **_: nullcontext(conn))
    calls = []
    monkeypatch.setattr(bin_label, "send_label_job", lambda job: calls.append(job))

    def unavailable(*args):
        raise psycopg.OperationalError("synthetic lost database connection")

    monkeypatch.setattr(bin_print, "_record_outcome", unavailable)
    job = bin_label.LabelJob("cups:synthetic-printer", "tspl", b"synthetic", 2)
    action = str(uuid4())
    result = bin_print.submit_label("AGR-123", action, job)
    assert result.status == "unknown"
    assert "could not be recorded" in result.error
    assert bin_print.submit_label("AGR-123", action, job).status == "unresolved"
    assert len(calls) == 1
    response = TestClient(bin_photo_web.create_app()).get("/manage/AGR-123")
    assert response.status_code == 200
    assert "Print status unknown" in response.text
    assert "synthetic-printer, 2 copies" in response.text
    assert "will not retry automatically" in response.text


def test_reservation_failure_never_reaches_printer(conn, monkeypatch):
    from binkeeper.bin_print import submit_label

    def unavailable(**kwargs):
        raise psycopg.OperationalError("synthetic disconnected database")

    monkeypatch.setattr(db, "connect", unavailable)
    monkeypatch.setattr(
        bin_label, "send_label_job", lambda job: pytest.fail("Unreserved printer attempt")
    )
    with pytest.raises(bin_label.BinLabelError, match="No label was submitted"):
        submit_label("AGR-123", str(uuid4()), bin_label.LabelJob("cups:fake", "tspl", b"x", 1))
