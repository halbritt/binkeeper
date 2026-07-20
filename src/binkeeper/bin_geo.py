"""GPS EXIF extraction + site-anchor persistence for BinKeeper registration.

The registration tap reads a photo's GPS from EXIF and resolves it to one of the
owner's named sites by proximity to per-site anchors (:func:`bin_harvest.resolve_site`,
which abstains on ambiguity). Coordinates are precise-location data: anchors live
in the gitignored ``sites.json`` (never git, never a DB column) and a fix rides
only in a capture's ``raw_payload.metadata.gps`` -- the same privacy posture the
rest of the bin-location model already keeps (D170 / RFC 0088).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from binkeeper.sites import DEFAULT_SITE_RADIUS_M, SITE_RADII_M

DEFAULT_SITES_FILE = Path.home() / ".config/binkeeper/sites.json"

# A bin code is a 2-4 letter site prefix, a hyphen, then digits (e.g. AGR-001).
_BIN_CODE_RE: Final[re.Pattern[str]] = re.compile(r"\b([A-Z]{2,4}-\d{2,4})\b")

# Sub-location anchors (BINK-30) reserve the LOC- prefix inside the same code
# grammar: LOC-014 prints, scans, and normalizes exactly like a bin code, but
# names a shelf/cabinet witness point, never a container.
ANCHOR_CODE_PREFIX: Final[str] = "LOC"

# The canonical site vocabulary (binkeeper.sites); used to seed a fresh
# sites.json so the owner only ever fills coordinates, never slugs.
_KNOWN_SITE_RADIUS_M: dict[str, float] = dict(SITE_RADII_M)


class BinGeoError(Exception):
    """Domain root for GPS/anchor failures (RFC 0012 per-stage family)."""


@dataclass(frozen=True)
class GpsFix:
    """A decoded photo GPS fix. ``accuracy_m`` is None when EXIF omits it."""

    lat: float
    lon: float
    accuracy_m: float | None = None


def extract_gps(image: bytes) -> GpsFix | None:
    """Read a GPS fix from a photo's EXIF, or None if absent/undecodable.

    Pure of side effects. Degrades to None (never raises) when Pillow is absent,
    the image is corrupt, or the photo carries no location -- the caller then
    falls back to an explicit site pick.
    """
    try:
        import io

        from PIL import ExifTags, Image
    except Exception:  # Pillow is a serve-extra
        return None
    try:
        with Image.open(io.BytesIO(image)) as img:
            exif = img.getexif()
            if not exif:
                return None
            gps_ifd_tag = next((k for k, v in ExifTags.TAGS.items() if v == "GPSInfo"), None)
            if gps_ifd_tag is None:
                return None
            gps = exif.get_ifd(gps_ifd_tag)
            if not gps:
                return None
            tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps.items()}
    except Exception:  # corrupt/undecodable image -> no fix
        return None
    lat = _dms_to_deg(tags.get("GPSLatitude"), tags.get("GPSLatitudeRef"))
    lon = _dms_to_deg(tags.get("GPSLongitude"), tags.get("GPSLongitudeRef"))
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None
    return GpsFix(lat=lat, lon=lon, accuracy_m=_to_float(tags.get("GPSHPositioningError")))


@dataclass(frozen=True)
class DecodedCode:
    """One decoded QR symbol: its normalized code and bounding rectangle."""

    code: str
    is_anchor: bool
    left: int
    top: int
    width: int
    height: int


def is_anchor_code(text: object) -> bool:
    """Return whether a code names a sub-location anchor (LOC- prefix)."""
    code = normalize_bin_code(text)
    return code is not None and code.startswith(f"{ANCHOR_CODE_PREFIX}-")


def decode_all_codes(image: bytes) -> list[DecodedCode]:
    """Read EVERY code visible in a photo, each with its bounding rect.

    pyzbar already returns all symbols plus geometry; this keeps them. A photo
    holding an anchor and two bin labels yields three entries whose rects later
    weight co-location strength (an anchor prints physically larger, so
    apparent size separates "on this shelf" from "in the same wide shot").
    Degrades to an empty list (never raises) when pyzbar/libzbar is absent or
    the image is unreadable.
    """
    try:
        import io

        from PIL import Image
        from pyzbar.pyzbar import decode as zbar_decode
    except Exception:  # pyzbar is a serve-extra; libzbar may be absent
        return []
    try:
        with Image.open(io.BytesIO(image)) as img:
            results = zbar_decode(img)
    except Exception:  # corrupt image / decode failure
        return []
    decoded: list[DecodedCode] = []
    for result in results:
        try:
            payload = result.data.decode("utf-8", "ignore")
        except Exception:
            continue
        code = normalize_bin_code(payload)
        if not code:
            continue
        rect = getattr(result, "rect", None)
        decoded.append(
            DecodedCode(
                code=code,
                is_anchor=is_anchor_code(code),
                left=int(getattr(rect, "left", 0)),
                top=int(getattr(rect, "top", 0)),
                width=int(getattr(rect, "width", 0)),
                height=int(getattr(rect, "height", 0)),
            )
        )
    return decoded


def decode_bin_code(image: bytes) -> str | None:
    """Read the first BIN code from a QR in the photo, or None if unreadable.

    The label prints a QR encoding the bare bin code precisely so a machine can
    read it -- this is the zero-typing path. Anchor codes (LOC-) are skipped:
    registration and management must never mistake a shelf witness point for a
    container. Degrades to None (never raises); the caller then falls back to
    vision OCR, then to an explicit field.
    """
    for decoded in decode_all_codes(image):
        if not decoded.is_anchor:
            return decoded.code
    return None


def normalize_bin_code(text: object) -> str | None:
    """Extract and upcase a well-formed bin code (e.g. 'agr-001' -> 'AGR-001')."""
    if not isinstance(text, str):
        return None
    match = _BIN_CODE_RE.search(text.strip().upper())
    return match.group(1) if match else None


def upsert_site_anchor(
    slug: str,
    lat: float,
    lon: float,
    *,
    path: str | Path = DEFAULT_SITES_FILE,
    radius_m: float | None = None,
    overwrite: bool = False,
) -> bool:
    """Establish (or re-survey) a site's geofence anchor in ``sites.json``.

    Returns True iff the file changed. By default only a still-null anchor is
    filled (the first registration at a site establishes it); ``overwrite=True``
    re-surveys an existing one. The file is written 0600 -- it holds precise
    location and is gitignored, matching the established privacy posture.
    """
    slug = slug.strip()
    if not slug:
        raise BinGeoError("site slug must be non-empty")
    sites_path = Path(path)
    data = _load_raw(sites_path)
    entry = data.get(slug)
    if not isinstance(entry, dict):
        default_radius = radius_m or _KNOWN_SITE_RADIUS_M.get(slug, DEFAULT_SITE_RADIUS_M)
        entry = {"lat": None, "lon": None, "radius_m": default_radius}
    already_set = entry.get("lat") is not None and entry.get("lon") is not None
    if already_set and not overwrite:
        return False
    entry["lat"] = round(float(lat), 6)
    entry["lon"] = round(float(lon), 6)
    if radius_m is not None:
        entry["radius_m"] = radius_m
    entry.setdefault("radius_m", _KNOWN_SITE_RADIUS_M.get(slug, DEFAULT_SITE_RADIUS_M))
    data[slug] = entry
    _write_raw(sites_path, data)
    return True


def _load_raw(sites_path: Path) -> dict[str, object]:
    if not sites_path.exists():
        # Seed from the known slugs (null coords) so the file stays self-describing.
        seeded: dict[str, object] = {
            "_README": "Per-site geofence anchors (precise location; gitignored). "
            "Filled as bins are registered via the tap."
        }
        for known, radius in _KNOWN_SITE_RADIUS_M.items():
            seeded[known] = {"lat": None, "lon": None, "radius_m": radius}
        return seeded
    try:
        raw = json.loads(sites_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BinGeoError(f"could not read sites file {sites_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BinGeoError(f"sites file {sites_path} is not a JSON object")
    return raw


def _write_raw(sites_path: Path, data: dict[str, object]) -> None:
    sites_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = sites_path.with_suffix(sites_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(sites_path)


def _dms_to_deg(dms: object, ref: object) -> float | None:
    try:
        d, m, s = (float(x) for x in dms)  # type: ignore[misc]
    except (TypeError, ValueError):
        return None
    deg = d + m / 60.0 + s / 3600.0
    if isinstance(ref, str) and ref.strip().upper() in ("S", "W"):
        deg = -deg
    return round(deg, 6)


def _to_float(value: object) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
