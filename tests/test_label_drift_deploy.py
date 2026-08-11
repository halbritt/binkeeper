"""Deployment contract for the ADR 0006 nightly two-model lane."""

from pathlib import Path


def test_label_drift_unit_claims_the_exact_local_model_and_runs_after_ocr() -> None:
    unit = Path("deploy/systemd/binkeeper-label-drift.service").read_text(encoding="utf-8")

    assert "After=postgresql.service network-online.target binkeeper-ocr-harvest.service" in unit
    assert "EnvironmentFile=/etc/binkeeper/binkeeper.env" in unit
    assert "EnvironmentFile=/etc/binkeeper/binkeeper-label-drift.env" in unit
    assert "/home/halbritt/git/gpu-fleet/bin/gpu-fleet-run --model qwen3-vl:8b" in unit
    assert "--max-context 32768 --latency-class batch" in unit
    assert "bin-label-drift-harvest" in unit
    assert "--local-model @@GPU_FLEET_SERVED_MODEL@@" in unit
    assert "--local-endpoint @@GPU_FLEET_ENDPOINT_URL@@" in unit


def test_label_drift_timer_and_pin_keep_the_nightly_ensemble_explicit() -> None:
    timer = Path("deploy/systemd/binkeeper-label-drift.timer").read_text(encoding="utf-8")
    pin = Path("deploy/systemd/binkeeper-label-drift.env").read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 04:00:00" in timer
    assert "RandomizedDelaySec=15m" in timer
    assert "Persistent=true" in timer
    assert "BINKEEPER_BIN_LABEL_DRIFT_MODE=ensemble" in pin
    assert "BINKEEPER_BIN_LABEL_DRIFT_CLOUD_MODEL=anthropic/claude-opus-5" in pin
