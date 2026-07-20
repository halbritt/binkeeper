"""BINK-36 — the site vocabulary has one source of truth (binkeeper.sites)."""

from __future__ import annotations

import re

from binkeeper import bin_geo, sites
from binkeeper.bin_photo_web import _SITE_PREFIXES


def test_slugs_and_prefixes_are_unique() -> None:
    slugs = [site.slug for site in sites.SITES]
    prefixes = [site.prefix for site in sites.SITES]
    assert len(set(slugs)) == len(slugs)
    assert len(set(prefixes)) == len(prefixes)


def test_prefixes_fit_the_bin_code_grammar() -> None:
    for site in sites.SITES:
        assert re.fullmatch(r"[A-Z]{2,4}", site.prefix), site.prefix
        assert bin_geo.normalize_bin_code(f"{site.prefix}-001") == f"{site.prefix}-001"


def test_photo_web_uses_the_canonical_prefix_map() -> None:
    assert _SITE_PREFIXES is sites.SITE_PREFIXES


def test_geo_seeds_from_the_canonical_radii() -> None:
    assert bin_geo._KNOWN_SITE_RADIUS_M == sites.SITE_RADII_M
    assert set(bin_geo._KNOWN_SITE_RADIUS_M) == set(sites.SITE_SLUGS)


def test_every_site_has_a_positive_radius() -> None:
    for site in sites.SITES:
        assert site.default_radius_m > 0
