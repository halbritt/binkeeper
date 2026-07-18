"""RFC 0088 T1b — tests for the bin label / QR generator (src/engram/bin_label.py).

TSPL rendering, label-size parsing, and the print-plan construction are all pure
or dry-run, so the whole suite runs without a printer or a database.
"""

from __future__ import annotations

import subprocess

import pytest

from binkeeper.bin_label import (
    BIN_LABEL_DPI,
    BIN_LABEL_PRINT_TIMEOUT_S,
    BinLabelError,
    PrintPlan,
    _fit_multiplier,
    _short_site,
    _wrap,
    code_check,
    label_for,
    render_tspl,
    send_to_printer,
)

# --- label geometry ---------------------------------------------------------


def test_label_for_known_preset():
    spec = label_for("4x6")
    assert (spec.width_in, spec.height_in) == (4.0, 6.0)
    assert spec.width_dots(203) == 812
    assert spec.height_dots(203) == 1218


def test_label_for_custom_wxh():
    spec = label_for("2.5x1.2")
    assert spec.width_in == pytest.approx(2.5)
    assert spec.height_in == pytest.approx(1.2)


def test_label_for_rejects_garbage_and_nonpositive():
    with pytest.raises(BinLabelError):
        label_for("banana")
    with pytest.raises(BinLabelError):
        label_for("0x5")


# --- TSPL rendering ---------------------------------------------------------


def test_render_tspl_has_setup_and_print_and_media_size():
    tspl = render_tspl("ALA-014", label=label_for("4x6"), dpi=203)
    assert tspl.startswith("SIZE 4,6")
    assert "DIRECTION 1" in tspl
    assert "CLS" in tspl
    assert tspl.rstrip().endswith("PRINT 1,1")
    # TSPL commands are CRLF-terminated.
    assert "\r\n" in tspl


def test_render_tspl_encodes_the_bare_code_in_the_qr_and_as_text():
    tspl = render_tspl("ALA-014")
    assert "QRCODE " in tspl
    assert '"ALA-014"' in tspl  # QR data carries the code verbatim
    assert "TEXT " in tspl
    assert tspl.count("ALA-014") >= 2  # once in the QR, once as text


def test_render_tspl_renders_site_as_an_inverted_return_to_home_band():
    tspl = render_tspl("FAB-003", site="oakland-fab-east", contents="TIG collets")
    assert "RETURN TO: OAK-FAB-EAST" in tspl  # site is abbreviated into the home band
    assert "REVERSE" in tspl  # the band is inverted (white-on-black)
    assert "TIG collets" in tspl


def test_render_tspl_prints_a_check_badge_next_to_the_code():
    tspl = render_tspl("ALA-014")
    assert f"({code_check('ALA-014')})" in tspl


def test_short_site_abbreviates_first_token():
    assert _short_site("alameda-garage") == "ALA-GARAGE"
    assert _short_site("oakland-fab-east") == "OAK-FAB-EAST"


def test_code_check_is_deterministic_two_chars_and_error_sensitive():
    assert len(code_check("ALA-014")) == 2
    assert code_check("ALA-014") == code_check("ALA-014")
    # A single-character mis-copy (O for 0) yields a different badge.
    assert code_check("ALA-014") != code_check("ALA-O14")


def test_render_tspl_theme_is_a_big_centered_hero_above_the_qr():
    import re

    tspl = render_tspl("ALA-014", theme="electric", label=label_for("4x6"))
    assert "ELECTRIC" in tspl  # uppercased grab-word
    # The theme prints with a font-4 multiplier of at least 2 (big), and...
    match = re.search(r'TEXT \d+,\d+,"4",0,(\d+),\d+,"ELECTRIC"', tspl)
    assert match is not None and int(match.group(1)) >= 2
    # ...it sits above the QR (hero on top).
    assert tspl.index("ELECTRIC") < tspl.index("QRCODE")


def test_render_tspl_theme_shrinks_to_fit_a_long_word():
    usable = label_for("4x6").width_dots() - 2 * max(16, round(203 * 0.08))
    assert _fit_multiplier("TIG", usable) > _fit_multiplier("FASTENERS-AND-WASHERS", usable)


def test_wrap_is_word_aware_and_bounds_line_length():
    lines = _wrap("alpha beta gamma delta", 11)
    assert all(len(line) <= 11 for line in lines)
    assert " ".join(lines).split() == ["alpha", "beta", "gamma", "delta"]


def test_wrap_hard_splits_an_oversized_token():
    lines = _wrap("SUPERCALIFRAGILISTIC bit", 8)
    assert all(len(line) <= 8 for line in lines)
    assert "".join(lines).startswith("SUPERCALIFRAGILISTIC")


def test_render_tspl_wraps_long_contents_across_multiple_lines_without_clipping():
    long_contents = (
        "2x Intel NUC: NUC10i7FNH + NUC5i5RYH; USB-C KVM dock, USB hub, "
        "mice, USB/Lightning cables, adapter, tape, pouches"
    )
    tspl = render_tspl("AGR-001", theme="MINI PC", site="alameda-garage", contents=long_contents)
    detail_words = ("NUC", "USB", "mice", "pouches")
    contents_lines = [
        ln
        for ln in tspl.splitlines()
        if '"2",0,1,1' in ln and any(word in ln for word in detail_words)
    ]
    assert len(contents_lines) >= 2  # it wrapped rather than emitting one clipped line
    # Both NUC models survive in the rendered TSPL (the detail the owner cares about).
    assert "NUC10i7FNH" in tspl
    assert "NUC5i5RYH" in tspl


def test_render_tspl_sanitizes_string_breaking_characters():
    tspl = render_tspl("BIN-1", contents='a"b\\c\nd')
    assert "a b c d" in tspl
    # The injected quote/backslash must not survive inside the data field.
    assert 'a"b' not in tspl


def test_render_tspl_sets_copies():
    assert "PRINT 1,3" in render_tspl("BIN-1", copies=3)


def test_render_tspl_rejects_empty_code_bad_ecc_and_copies():
    with pytest.raises(BinLabelError):
        render_tspl("   ")
    with pytest.raises(BinLabelError):
        render_tspl("BIN-1", qr_ecc="Z")
    with pytest.raises(BinLabelError):
        render_tspl("BIN-1", copies=0)


def test_default_dpi_is_203():
    assert BIN_LABEL_DPI == 203


# --- print plan (dry-run) ---------------------------------------------------


def test_send_to_printer_dry_run_device():
    plan = send_to_printer("SIZE 4,6\r\n", device="/dev/usb/lp1", dry_run=True)
    assert isinstance(plan, PrintPlan)
    assert plan.transport == "dry-run"
    assert plan.target == "device:/dev/usb/lp1"
    assert plan.byte_count == len("SIZE 4,6\r\n")


def test_send_to_printer_dry_run_cups():
    plan = send_to_printer("SIZE 4,6\r\n", cups_queue="OmezizyD450", dry_run=True)
    assert plan.target == "cups:OmezizyD450"


def test_send_to_printer_requires_exactly_one_target():
    with pytest.raises(BinLabelError):
        send_to_printer("SIZE 4,6\r\n", dry_run=True)
    with pytest.raises(BinLabelError):
        send_to_printer("SIZE 4,6\r\n", device="/dev/usb/lp1", cups_queue="q", dry_run=True)


def test_send_to_printer_bounds_cups_submission_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> None:
        captured.update(kwargs)
        raise subprocess.TimeoutExpired(cmd="lp", timeout=float(kwargs["timeout"]))

    monkeypatch.setattr("binkeeper.bin_label.subprocess.run", fake_run)

    with pytest.raises(BinLabelError, match="timed out"):
        send_to_printer("SIZE 4,6\r\n", cups_queue="OmezizyD450")

    assert captured["timeout"] == BIN_LABEL_PRINT_TIMEOUT_S


def test_send_to_printer_translates_cups_process_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> None:
        raise OSError("spool transport unavailable")

    monkeypatch.setattr("binkeeper.bin_label.subprocess.run", fake_run)

    with pytest.raises(BinLabelError, match="could not run lp"):
        send_to_printer("SIZE 4,6\r\n", cups_queue="OmezizyD450")
