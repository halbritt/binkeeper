"""Render and submit small Niimbot B1 labels over local Bluetooth."""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import cast

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H

from binkeeper.bin_label import BinLabelError, PrintPlan, code_check

B1_WIDTH_DOTS = 384
B1_HEIGHT_DOTS = 240
B1_ADDRESS = os.environ.get("BINKEEPER_BIN_LABEL_B1_ADDRESS", "").strip()
B1_TIMEOUT_S = float(os.environ.get("BINKEEPER_BIN_LABEL_B1_TIMEOUT_S", "60"))


class B1PrintUnknown(BinLabelError):
    """The worker started; a label may have advanced before it failed."""


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("DejaVuSans.ttf", size)


def _fit(
    draw: ImageDraw.ImageDraw, text: str, max_width: int, max_size: int
) -> ImageFont.FreeTypeFont:
    for size in range(max_size, 9, -1):
        font = _font(size)
        if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
            return font
    return _font(10)


def _shorten(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= width:
        return text
    while text and draw.textbbox((0, 0), text + "…", font=font)[2] > width:
        text = text[:-1]
    return text + "…"


def render_b1_png(
    bin_code: str,
    *,
    theme: str | None = None,
    site: str | None = None,
    contents: str | None = None,
) -> bytes:
    """Return a 203 dpi monochrome PNG with the bare bin code in its QR."""
    code = bin_code.strip()
    if not code:
        raise BinLabelError("B1 label requires a non-empty bin code")
    width, height = B1_WIDTH_DOTS, B1_HEIGHT_DOTS
    image = Image.new("1", (width, height), 1)
    draw = ImageDraw.Draw(image)
    margin = 8
    title = (theme or code).strip().upper()
    title_font = _fit(draw, title, width - 2 * margin, min(42, height // 5))
    draw.text((margin, margin), title, font=title_font, fill=0)
    title_bottom = margin + int(draw.textbbox((0, 0), title, font=title_font)[3]) + 6
    draw.line((margin, title_bottom, width - margin, title_bottom), fill=0, width=2)

    qr_side = min(120, height - title_bottom - 2 * margin)
    if qr_side < 48:
        raise BinLabelError("B1 label is too short for a readable QR")
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_H, border=4)
    qr.add_data(code)
    qr.make(fit=True)
    qr_image = cast(Image.Image, qr.make_image(fill_color="black", back_color="white").get_image())
    qr_image = qr_image.convert("1")
    qr_image = qr_image.resize((qr_side, qr_side), Image.Resampling.NEAREST)
    image.paste(qr_image, (margin, title_bottom + 4))

    text_x = margin + qr_side + 8
    text_width = width - text_x - margin
    code_font = _fit(draw, code, text_width, 27)
    draw.text((text_x, title_bottom + 8), code, font=code_font, fill=0)
    small_font = _font(15)
    line_y = title_bottom + 42
    draw.text((text_x, line_y), f"({code_check(code)})", font=small_font, fill=0)
    if site:
        line_y += 25
        draw.text(
            (text_x, line_y),
            _shorten(draw, site.strip().upper(), small_font, text_width),
            font=small_font,
            fill=0,
        )
    if contents:
        line_y += 23
        if line_y + 18 < height - margin:
            draw.text(
                (text_x, line_y),
                _shorten(draw, contents.strip(), small_font, text_width),
                font=small_font,
                fill=0,
            )

    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def send_b1(payload: bytes, *, address: str = B1_ADDRESS, copies: int = 1) -> PrintPlan:
    """Submit one bounded local B1 worker. A timeout leaves print status unknown."""
    if not address:
        raise BinLabelError("No B1 Bluetooth address is configured")
    if copies not in (1, 2):
        raise BinLabelError("B1 label count must be one or two")
    with tempfile.TemporaryDirectory(prefix="binkeeper-b1-") as directory:
        image_path = Path(directory) / "label.png"
        image_path.write_bytes(payload)
        command = [
            sys.executable,
            "-m",
            "binkeeper.bin_b1_worker",
            "--address",
            address,
            "--copies",
            str(copies),
            "--image",
            str(image_path),
        ]
        try:
            completed = subprocess.run(
                command, capture_output=True, check=False, timeout=B1_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as exc:
            raise B1PrintUnknown("B1 print status is unknown after timeout") from exc
        except OSError as exc:
            raise BinLabelError(f"Could not start B1 printer command: {exc}") from exc
        if completed.returncode:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise B1PrintUnknown(f"B1 print status is unknown: {detail or completed.returncode}")
    return PrintPlan("niimbot-b1", address, len(payload))
