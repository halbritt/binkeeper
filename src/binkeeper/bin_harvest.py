"""Local site-anchor loading used by GPS registration."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SITES_FILE = Path.home() / ".config/binkeeper/sites.json"


class BinHarvestError(ValueError):
    """A local site-anchor file is malformed."""


@dataclass(frozen=True)
class SiteAnchor:
    lat: float | None
    lon: float | None
    radius_m: float | None = None


def load_sites(path: str | os.PathLike[str] = DEFAULT_SITES_FILE) -> dict[str, SiteAnchor]:
    sites_path = Path(path)
    if not sites_path.exists():
        return {}
    try:
        raw = json.loads(sites_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BinHarvestError(f"could not read sites file {sites_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise BinHarvestError(f"sites file {sites_path} must be a JSON object")
    sites: dict[str, SiteAnchor] = {}
    for slug, anchor in raw.items():
        if slug.startswith("_") or not isinstance(anchor, dict):
            continue
        lat, lon, radius = anchor.get("lat"), anchor.get("lon"), anchor.get("radius_m")
        sites[slug] = SiteAnchor(
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            radius_m=float(radius) if radius is not None else None,
        )
    return sites
