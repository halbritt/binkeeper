"""Shared validation and thumbnail rules for BinKeeper photos."""

from __future__ import annotations

from io import BytesIO
from typing import Final

from PIL import Image, ImageOps

BIN_PHOTO_THUMBNAIL_SIZE: Final[tuple[int, int]] = (960, 720)


class BinPhotoMediaError(ValueError):
    """Raised when uploaded bytes cannot become a catalog thumbnail."""


def validate_bin_photo(original: bytes) -> None:
    """Reject bytes the catalog cannot safely decode and normalize."""
    try:
        thumbnail_jpeg(original)
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise BinPhotoMediaError("photo is corrupt, unsupported, or too large") from exc


def thumbnail_jpeg(original: bytes) -> bytes:
    """Normalize an original to a bounded JPEG without carrying metadata."""
    with Image.open(BytesIO(original)) as opened, ImageOps.exif_transpose(opened) as oriented:
        oriented.thumbnail(BIN_PHOTO_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        with BytesIO() as output, oriented.convert("RGB") as thumbnail:
            thumbnail.save(output, format="JPEG", quality=84, optimize=True)
            return output.getvalue()
