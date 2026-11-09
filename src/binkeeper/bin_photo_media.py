"""Shared validation and thumbnail rules for BinKeeper photos."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO
from typing import Final

from PIL import Image, ImageOps

BIN_PHOTO_THUMBNAIL_SIZE: Final[tuple[int, int]] = (960, 720)
MAX_LABEL_PHOTOS: Final[int] = 12
MAX_PHOTO_BYTES: Final[int] = 20 * 1024 * 1024


class BinPhotoMediaError(ValueError):
    """Raised when uploaded bytes cannot become a catalog thumbnail."""


def validate_bin_photo(original: bytes) -> None:
    """Reject bytes the catalog cannot safely decode and normalize."""
    if not original or len(original) > MAX_PHOTO_BYTES:
        raise BinPhotoMediaError("Each photo must be nonempty and at most 20 MiB.")
    try:
        thumbnail_jpeg(original)
    except (Image.DecompressionBombError, OSError, SyntaxError, ValueError) as exc:
        raise BinPhotoMediaError("photo is corrupt, unsupported, or too large") from exc


def validate_bin_photos(images: Sequence[bytes]) -> None:
    if not 1 <= len(images) <= MAX_LABEL_PHOTOS:
        raise BinPhotoMediaError(f"Choose between 1 and {MAX_LABEL_PHOTOS} photos of one bin.")
    for original in images:
        validate_bin_photo(original)


def thumbnail_jpeg(original: bytes) -> bytes:
    """Normalize an original to a bounded JPEG without carrying metadata."""
    with Image.open(BytesIO(original)) as opened, ImageOps.exif_transpose(opened) as oriented:
        oriented.thumbnail(BIN_PHOTO_THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
        with BytesIO() as output, oriented.convert("RGB") as thumbnail:
            thumbnail.save(output, format="JPEG", quality=84, optimize=True)
            return output.getvalue()
