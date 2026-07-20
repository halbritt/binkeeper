"""Canonical site vocabulary — the single source of truth (BINK-36).

Slugs, bin-code prefixes, and default geofence radii live here and nowhere
else; the photo/registration surfaces and the GPS geofence module both import
this table. Coordinates never appear here: precise anchors stay in the
gitignored ``sites.json`` (:mod:`binkeeper.bin_geo`), matching the established
privacy posture (D170 / RFC 0088).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class Site:
    """One canonical owner site."""

    slug: str
    prefix: str
    default_radius_m: float


SITES: Final[tuple[Site, ...]] = (
    Site("alameda-garage", "AGR", 120),
    Site("alameda-storage", "AST", 120),
    Site("alameda-home", "AHM", 120),
    Site("cargo-trailer", "TRL", 60),
    Site("oakland-fab-east", "OFE", 150),
    Site("oakland-wood-west", "OWW", 150),
    Site("oakland-oldhome", "OOH", 150),
)

SITE_SLUGS: Final[tuple[str, ...]] = tuple(site.slug for site in SITES)
SITE_PREFIXES: Final[dict[str, str]] = {site.slug: site.prefix for site in SITES}
SITE_RADII_M: Final[dict[str, float]] = {site.slug: site.default_radius_m for site in SITES}
DEFAULT_SITE_RADIUS_M: Final[float] = 150.0
