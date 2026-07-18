"""RFC 0088 T1b — bin labels: render a QR + human-readable label as TSPL and print it.

RFC 0088 §5 fixes bin identity as a short, site-prefixed string (``ALA-014``)
"printed on a label *and* etched on the lid", and "a future QR encodes the same
string". This is that front end: it turns a bin code into a thermal label.

The owner's printer (an Omezizy/AIMO D450BT, USB id 2e3c:5756 "QIN LabelPrinter")
reports its command set as ``CMD:XPP,XL`` and is driven by the vendor's
``rastertolabeltspl`` CUPS filter, i.e. its wire language is **TSPL** (the TSC
family), not ZPL. So this emits TSPL: ``SIZE`` / ``GAP`` / ``DIRECTION`` / ``CLS``
set up the label, the printer draws the QR itself (``QRCODE``) and the text
(``TEXT``), and ``PRINT`` commits. That needs **zero rendering dependencies** and
the whole label is unit-testable without a printer. Verified 2026-06-25: a TSPL
label printed on the D450BT over ``/dev/usb/lp1`` and the QR scanned.

Generation is database-free: you print labels *before* you capture bins, so it
labels any code you hand it whether or not that bin exists yet.

Layering (RFC 0012 pure/IO split):
- Pure: ``render_tspl`` / ``label_for`` / ``LabelSpec`` — total functions that turn
  a code + site into a TSPL string, unit-tested without a printer.
- IO: ``default_device`` / ``send_to_printer`` — write the bytes to a raw device
  node or a CUPS raw queue.
"""

from __future__ import annotations

import glob
import os
import subprocess
from dataclasses import dataclass
from typing import Final

# --- tunables (ENGRAM_-prefixed, read at module top per RFC 0012) -----------

# The D450BT is a 203-dpi (8 dots/mm) head; TSPL coordinates are in dots.
BIN_LABEL_DPI: Final[int] = int(os.environ.get("ENGRAM_BIN_LABEL_DPI", "203"))
# Default media: the 4x6 stock the printer ships with. Override per call / on the CLI.
BIN_LABEL_DEFAULT_SIZE: Final[str] = os.environ.get("ENGRAM_BIN_LABEL_SIZE", "4x6")
# QR error-correction level (L/M/Q/H). H is most damage-tolerant for a shop label.
BIN_LABEL_QR_ECC: Final[str] = os.environ.get("ENGRAM_BIN_LABEL_QR_ECC", "H").upper()
# Inter-label gap in inches for the GAP command. 0 = treat media as continuous
# (verified working on the bundled 4x6 die-cut stock); set to the real gap (~0.12)
# if smaller die-cut labels drift over a long run.
BIN_LABEL_GAP_IN: Final[float] = float(os.environ.get("ENGRAM_BIN_LABEL_GAP_IN", "0"))
# Default raw device node for --print when no target is given. Empty => autodetect
# the first /dev/usb/lp* (a usblp printer-class node).
BIN_LABEL_DEVICE: Final[str] = os.environ.get("ENGRAM_BIN_LABEL_DEVICE", "")
# Optional raw CUPS queue used by the reviewed BinKeeper web action. Empty keeps
# browser printing unavailable until an operator selects an explicit local queue.
BIN_LABEL_CUPS_QUEUE: Final[str] = os.environ.get("ENGRAM_BIN_LABEL_CUPS_QUEUE", "")
# Bound the synchronous CUPS handoff so a wedged local spooler cannot leave the
# owner-facing request busy forever after registration has already committed.
BIN_LABEL_PRINT_TIMEOUT_S: Final[float] = float(
    os.environ.get("ENGRAM_BIN_LABEL_PRINT_TIMEOUT_S", "15")
)


class BinLabelError(Exception):
    """Domain root for label rendering / printing failures (RFC 0012 per-stage family)."""


# --- label geometry ---------------------------------------------------------


@dataclass(frozen=True)
class LabelSpec:
    """A label's physical size, in inches, plus a name for the CLI."""

    name: str
    width_in: float
    height_in: float

    def width_dots(self, dpi: int = BIN_LABEL_DPI) -> int:
        """Label width in printer dots."""
        return round(self.width_in * dpi)

    def height_dots(self, dpi: int = BIN_LABEL_DPI) -> int:
        """Label length in printer dots."""
        return round(self.height_in * dpi)


# Named presets. The D450BT handles 1"-4.6" wide media; these cover bins (small)
# through the bundled 4x6 shipping stock. A custom "WxH" is parsed by ``label_for``.
LABEL_PRESETS: Final[dict[str, LabelSpec]] = {
    "4x6": LabelSpec("4x6", 4.0, 6.0),
    "2x1": LabelSpec("2x1", 2.0, 1.0),
    "2.25x1.25": LabelSpec("2.25x1.25", 2.25, 1.25),
    "3x2": LabelSpec("3x2", 3.0, 2.0),
    "1.5x1": LabelSpec("1.5x1", 1.5, 1.0),
}


def label_for(size: str) -> LabelSpec:
    """Resolve a label size: a preset name, or a custom ``WxH`` in inches. Pure.

    ``"4x6"`` -> the bundled shipping stock; ``"2.5x1.2"`` -> a custom 2.5 by 1.2
    inch label. Raises ``BinLabelError`` on an unparseable size.
    """
    key = size.strip().lower()
    if key in LABEL_PRESETS:
        return LABEL_PRESETS[key]
    if "x" in key:
        left, _, right = key.partition("x")
        try:
            width_in = float(left)
            height_in = float(right)
        except ValueError as exc:
            raise BinLabelError(
                f"could not parse label size {size!r} (expected WxH in inches)"
            ) from exc
        if width_in <= 0 or height_in <= 0:
            raise BinLabelError(f"label size {size!r} must be positive")
        return LabelSpec(f"{width_in:g}x{height_in:g}", width_in, height_in)
    raise BinLabelError(
        f"unknown label size {size!r}; known presets: {', '.join(sorted(LABEL_PRESETS))} or 'WxH'"
    )


# --- pure TSPL rendering ----------------------------------------------------


def _sanitize(text: str) -> str:
    """Strip TSPL string-breaking characters from a data field. Pure and total.

    A double quote delimits a TSPL string and a backslash is its escape char, and
    a newline ends a command, so those are replaced with spaces; the result is
    trimmed. Bin codes / sites never contain these; a free-text contents note might.
    """
    cleaned = text.replace('"', " ").replace("\\", " ").replace("\n", " ").replace("\r", " ")
    return cleaned.strip()


def _qr_modules_estimate(cell: int) -> int:
    """Rough on-label QR side length in dots, for laying text out beside it.

    A short code is a version 1-2 symbol (~25 modules); each module is ``cell``
    dots. Deliberately an over-estimate so text never overlaps the QR.
    """
    return 25 * cell


# Built-in TSPL font "4" is ~24x32 dots; ~26 dots of advance per character at
# multiplier 1. Used to size the hero theme line to the label width.
_FONT4_ADVANCE: Final[int] = 26
_FONT4_HEIGHT: Final[int] = 32
# Built-in TSPL font "2" is ~12x20 dots; ~14 dots of advance per character at
# multiplier 1. Used for the small contents detail, which is word-wrapped.
_FONT2_ADVANCE: Final[int] = 14
_FONT2_HEIGHT: Final[int] = 20


def _wrap(text: str, max_chars: int) -> list[str]:
    """Greedy word-wrap ``text`` to lines of at most ``max_chars``. Pure and total.

    Whitespace-collapsing and word-aware; a single token longer than the budget
    is hard-split so it can never overflow the media edge.
    """
    if max_chars < 1:
        max_chars = 1
    lines: list[str] = []
    current = ""
    for word in text.split():
        while len(word) > max_chars:  # a token wider than the label: hard-split it
            if current:
                lines.append(current)
                current = ""
            lines.append(word[:max_chars])
            word = word[max_chars:]
        candidate = f"{current} {word}".strip()
        if len(candidate) > max_chars:
            if current:
                lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_multiplier(text: str, usable_dots: int, *, max_mul: int = 6) -> int:
    """Largest font-4 multiplier that fits ``text`` on one line in ``usable_dots``.

    Lets the theme print as big as the label allows so it reads across a room; a
    long theme shrinks rather than overflowing. Pure and total.
    """
    if not text:
        return 1
    fit = usable_dots // (len(text) * _FONT4_ADVANCE)
    return max(1, min(max_mul, fit))


def _short_site(site: str) -> str:
    """Abbreviate a site slug for the home band. Pure and total.

    Three-letter first token, the rest kept, all uppercased: ``alameda-garage`` ->
    ``ALA-GARAGE``; ``oakland-fab-east`` -> ``OAK-FAB-EAST``. Keeps the home band
    compact so it reads big without a hand-maintained abbreviation table.
    """
    parts = [p for p in site.strip().split("-") if p]
    if not parts:
        return site.strip().upper()
    return "-".join([parts[0][:3].upper(), *(p.upper() for p in parts[1:])])


_CHECK_ALPHABET: Final[str] = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def code_check(code: str) -> str:
    """A 2-char base-36 check token for a bin code. Pure and deterministic.

    Lets a human confirm the printed label, the etched-lid code, and a scan all
    resolve to the same bin: matching badges agree, a differing badge flags a
    single-character mis-copy. Not an identifier and not cryptographic.
    """
    acc = 0
    for ch in code.upper():
        acc = (acc * 31 + ord(ch)) % 1296  # 36 * 36
    return _CHECK_ALPHABET[acc // 36] + _CHECK_ALPHABET[acc % 36]


def render_tspl(
    bin_code: str,
    *,
    theme: str | None = None,
    site: str | None = None,
    contents: str | None = None,
    label: LabelSpec | None = None,
    dpi: int = BIN_LABEL_DPI,
    copies: int = 1,
    qr_ecc: str = BIN_LABEL_QR_ECC,
    qr_cell: int | None = None,
    gap_in: float = BIN_LABEL_GAP_IN,
) -> str:
    """Render one bin label as a TSPL string. Pure and total.

    The **theme** is the hero: a big, centered grab-word (e.g. ``ELECTRIC``) that
    reads across a room, so the owner grabs the right bin without scanning. Below
    it sit the QR (encoding the bare bin code, RFC 0088 §5), the human-readable
    code, the site, and the small detailed contents line. Text sizes scale to the
    label and the theme auto-fits its width. The contract is valid TSPL carrying
    the code, the QR data, and the right media size.
    """
    code = _sanitize(bin_code)
    if not code:
        raise BinLabelError("render_tspl requires a non-empty bin_code")
    if copies < 1:
        raise BinLabelError("copies must be >= 1")
    if qr_ecc not in ("L", "M", "Q", "H"):
        raise BinLabelError(f"qr_ecc must be one of L/M/Q/H, got {qr_ecc!r}")
    spec = label or label_for(BIN_LABEL_DEFAULT_SIZE)
    width = spec.width_dots(dpi)
    margin = max(16, round(dpi * 0.08))  # ~0.08" quiet zone
    usable = width - 2 * margin

    lines: list[str] = [
        f"SIZE {spec.width_in:g},{spec.height_in:g}",
        f"GAP {gap_in:g},0",
        "DIRECTION 1",
        "REFERENCE 0,0",
        "CLS",
    ]
    y = margin

    # Home band: the bin's site as an inverted RETURN-TO band (TSPL REVERSE), so its
    # home reads as a fade-resistant landmark and a misplaced bin is obvious at a glance.
    clean_site = _sanitize(site) if site else ""
    if clean_site:
        band_label = f"RETURN TO: {_short_site(clean_site)}"
        band_mul = _fit_multiplier(band_label, usable, max_mul=3)
        band_h = 24 * band_mul + 20  # font "3" is ~24 dots tall, plus padding
        lines.append(f'TEXT {margin + 12},{y + 10},"3",0,{band_mul},{band_mul},"{band_label}"')
        lines.append(f"REVERSE {margin},{y},{usable},{band_h}")
        y += band_h + max(10, dpi // 16)

    # Theme hero: the across-the-room grab word (uppercased, centered, as big as fits).
    clean_theme = _sanitize(theme).upper() if theme else ""
    if clean_theme:
        theme_mul = _fit_multiplier(clean_theme, usable)
        theme_w = len(clean_theme) * _FONT4_ADVANCE * theme_mul
        theme_x = max(margin, (width - theme_w) // 2)
        lines.append(f'TEXT {theme_x},{y},"4",0,{theme_mul},{theme_mul},"{clean_theme}"')
        y += _FONT4_HEIGHT * theme_mul + max(10, dpi // 12)
        lines.append(f"BAR {margin},{y},{usable},4")  # rule under the theme
        y += 16

    # Identity block: QR on the left, bin code + a check-badge to its right.
    cell = qr_cell if qr_cell is not None else max(4, min(10, width // 90))
    qr_side = _qr_modules_estimate(cell)
    code_mul = max(1, min(4, round(width / 420)))
    lines.append(f'QRCODE {margin},{y},{qr_ecc},{cell},A,0,"{code}"')
    code_x = margin + qr_side + margin
    lines.append(f'TEXT {code_x},{y},"4",0,{code_mul},{code_mul},"{code}"')
    # Check-badge: a derived token so label, etched lid, and scan provably agree.
    badge_x = code_x + len(code) * 24 * code_mul + 20
    badge_y = y + max(0, _FONT4_HEIGHT * code_mul - 20)
    lines.append(f'TEXT {badge_x},{badge_y},"2",0,1,1,"({code_check(code)})"')
    y += qr_side + max(10, margin)

    # Contents detail (small): the full list, word-wrapped across as many lines as
    # the remaining label height allows so nothing clips at the media edge.
    if contents:
        clean_contents = _sanitize(contents)
        if clean_contents:
            char_budget = max(8, usable // _FONT2_ADVANCE)
            line_step = _FONT2_HEIGHT + 8
            height = spec.height_dots(dpi)
            max_lines = max(1, (height - margin - y) // line_step)
            wrapped = _wrap(clean_contents, char_budget)
            if len(wrapped) > max_lines and max_lines >= 1:
                # Never drop text silently: fold the overflow into the last line
                # and mark it with an ellipsis so the truncation is visible.
                kept = wrapped[: max_lines - 1]
                kept.append((wrapped[max_lines - 1] + " ...")[:char_budget])
                wrapped = kept
            for line_text in wrapped:
                lines.append(f'TEXT {margin},{y},"2",0,1,1,"{line_text}"')
                y += line_step

    lines.append(f"PRINT 1,{copies}")
    return "\r\n".join(lines) + "\r\n"


# --- IO: send raw TSPL to the printer ---------------------------------------


@dataclass(frozen=True)
class PrintPlan:
    """What ``send_to_printer`` did (or would do, when dry-run)."""

    transport: str  # "device" | "cups" | "dry-run"
    target: str
    byte_count: int

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the print result."""
        return {"transport": self.transport, "target": self.target, "byte_count": self.byte_count}


def default_device() -> str | None:
    """The raw device node to print to when none is given.

    ``ENGRAM_BIN_LABEL_DEVICE`` if set, else the first ``/dev/usb/lp*`` usblp node
    (a USB printer-class device), else None.
    """
    if BIN_LABEL_DEVICE:
        return BIN_LABEL_DEVICE
    nodes = sorted(glob.glob("/dev/usb/lp*"))
    return nodes[0] if nodes else None


def send_to_printer(
    data: str,
    *,
    device: str | None = None,
    cups_queue: str | None = None,
    dry_run: bool = False,
) -> PrintPlan:
    """Send raw label data to the printer, by device node or a CUPS raw queue.

    A USB printer-class device usually appears as ``/dev/usb/lp0`` / ``lp1`` (the
    kernel ``usblp`` driver): pass ``device=/dev/usb/lp1`` to write the bytes
    straight to it (needs membership in the ``lp`` group, or root). Or pass
    ``cups_queue`` to spool through a raw CUPS queue (``lp -d <queue> -o raw``).
    ``dry_run`` reports the plan without sending, so the caller can preview before
    any hardware is attached.
    """
    payload = data.encode("utf-8")
    if device and cups_queue:
        raise BinLabelError("pass only one of device / cups_queue")
    if not device and not cups_queue:
        raise BinLabelError("send_to_printer needs a device path or a cups_queue")
    if device:
        if dry_run:
            return PrintPlan("dry-run", f"device:{device}", len(payload))
        try:
            with open(device, "wb") as handle:
                handle.write(payload)
        except OSError as exc:
            raise BinLabelError(f"could not write to printer device {device}: {exc}") from exc
        return PrintPlan("device", device, len(payload))
    queue = cups_queue or ""
    if dry_run:
        return PrintPlan("dry-run", f"cups:{queue}", len(payload))
    try:
        completed = subprocess.run(
            ["lp", "-d", queue, "-o", "raw"],
            input=payload,
            capture_output=True,
            check=False,
            timeout=BIN_LABEL_PRINT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise BinLabelError("`lp` not found; install CUPS or use --device") from exc
    except subprocess.TimeoutExpired as exc:
        raise BinLabelError(
            f"lp timed out after {BIN_LABEL_PRINT_TIMEOUT_S:g}s for queue {queue!r}"
        ) from exc
    except OSError as exc:
        raise BinLabelError(f"could not run lp for queue {queue!r}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise BinLabelError(f"lp failed for queue {queue!r}: {detail or completed.returncode}")
    return PrintPlan("cups", queue, len(payload))
