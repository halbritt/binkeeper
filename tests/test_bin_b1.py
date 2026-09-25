"""Synthetic B1 label rendering and bounded local command behavior."""

from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image
from pyzbar.pyzbar import decode

from binkeeper import bin_b1, bin_label


def test_b1_png_has_printhead_dimensions_and_scannable_bare_code() -> None:
    payload = bin_b1.render_b1_png(
        "AGR-014", theme="HAND TOOLS", site="alameda-garage", contents="hex keys"
    )
    image = Image.open(io.BytesIO(payload))
    assert image.size == (384, 240)
    assert image.mode == "1"
    assert [symbol.data for symbol in decode(image)] == [b"AGR-014"]


def test_b1_job_uses_png_and_selected_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bin_label, "BIN_LABEL_PRINTER", "niimbot-b1")
    monkeypatch.setattr(bin_b1, "B1_ADDRESS", "01:23:45:67:89:AB")
    job = bin_label.make_label_job("AGR-014", theme="TOOLS", copies=2)
    assert job.target == "niimbot-b1:01:23:45:67:89:AB"
    assert job.format == "png"
    assert job.copies == 2
    assert job.payload.startswith(b"\x89PNG")


def test_b1_command_is_bounded_and_timeout_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = bin_b1.render_b1_png("AGR-014")
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        assert kwargs["timeout"] == bin_b1.B1_TIMEOUT_S
        assert command[1:8] == [
            "-m", "binkeeper.bin_b1_worker", "--address", "01:23:45:67:89:AB",
            "--copies", "2", "--image"
        ]
        with open(command[-1], "rb") as label_file:
            assert label_file.read() == payload
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(bin_b1.subprocess, "run", fake_run)
    plan = bin_b1.send_b1(payload, address="01:23:45:67:89:AB", copies=2)
    assert plan.transport == "niimbot-b1"
    assert len(calls) == 1

    def timed_out(command: list[str], **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(command, 45)

    monkeypatch.setattr(bin_b1.subprocess, "run", timed_out)
    with pytest.raises(bin_b1.B1PrintUnknown, match="unknown") as failure:
        bin_b1.send_b1(payload, address="01:23:45:67:89:AB")
    assert isinstance(failure.value.__cause__, subprocess.TimeoutExpired)
