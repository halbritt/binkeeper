from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from binkeeper.advisory_status import load_status, record_finish, record_start
from binkeeper.bin_catalog_web import create_app
from binkeeper.bin_label_drift import default_label_drift_client
from binkeeper.bin_vision import BinVisionError

START = datetime(2026, 11, 9, 12, tzinfo=UTC)


@pytest.mark.parametrize("key", [None, "synthetic-api-key"])
def test_old_ensemble_config_is_refused_even_with_an_api_key(monkeypatch, key):
    if key:
        monkeypatch.setenv("BINKEEPER_OPENROUTER_API_KEY", key)
    else:
        monkeypatch.delenv("BINKEEPER_OPENROUTER_API_KEY", raising=False)
    with pytest.raises(BinVisionError, match="OpenRouter fallback is forbidden"):
        default_label_drift_client(mode="ensemble")
    client = default_label_drift_client(
        mode="local", local_model="synthetic-local", local_endpoint="http://127.0.0.1:19999/v1"
    )
    assert client.model == "synthetic-local"


@pytest.mark.parametrize(
    "exit_status,reason",
    [("75", "leased"), ("5", "analyzed"), ("124", "timed out"), ("TERM", "did not complete")],
)
def test_failure_and_resume_preserve_last_success_and_failure(tmp_path, exit_status, reason):
    path = tmp_path / "status.json"
    record_start(path, now=START)
    assert load_status(path, now=START).state == "running"
    record_finish(path, service_result="success", exit_code="exited", exit_status="0", now=START)
    record_start(path, now=START + timedelta(days=1))
    record_finish(
        path,
        service_result="exit-code",
        exit_code="exited",
        exit_status=exit_status,
        now=START + timedelta(days=1),
    )
    failed = load_status(path, now=START + timedelta(days=1))
    assert failed.state == "degraded" and reason in failed.reason
    assert failed.last_success == START.isoformat()
    record_start(path, now=START + timedelta(days=2))
    record_finish(
        path,
        service_result="success",
        exit_code="exited",
        exit_status="0",
        now=START + timedelta(days=2),
    )
    resumed = load_status(path, now=START + timedelta(days=2))
    assert resumed.state == "current"
    assert resumed.last_failure == (START + timedelta(days=1)).isoformat()
    assert load_status(path, now=START + timedelta(days=4)).state == "stale"


def test_unknown_corrupt_and_interrupted_status_do_not_block_catalog(tmp_path):
    path = tmp_path / "status.json"
    app = create_app(
        passport_loader=lambda: [], advisory_status_loader=lambda: load_status(path, now=START)
    )
    for content in (None, "broken json", '{"state":"success","last_success":"bad-time"}'):
        if content:
            path.write_text(content)
        response = TestClient(app).get("/")
        assert response.status_code == 200
        assert "Nightly photo review: status unavailable" in response.text
    record_start(path, now=START - timedelta(hours=5))
    assert load_status(path, now=START).state == "stale"


def test_unreadable_photos_do_not_report_a_successful_review():
    from binkeeper.cli import label_drift_exit_code

    assert label_drift_exit_code({"skipped_unreadable_photos": 1}) == 5
