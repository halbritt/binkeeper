"""RFC 0088 T3b — photo-GPS harvester: corroborate bin locations from capture GPS.

The first passive-harvest source, and pure backfill — it reads the GPS already riding
in every bin capture (``captures.raw_payload.metadata.gps``, the T1 contract) and
records a *corroborative-only* ``location_observation`` for each, with **zero new
sensing**. An observation can only refresh confidence in the move ledger's existing
site; it never relocates a bin (the T3b invariant, enforced in ``bin_inventory``).

The load-bearing risk is the geofence (RFC 0088 §4): a coarse/overlapping geofence
fires false signals and trains the owner to ignore the surface. So this harvester
**abstains on geofence ambiguity** — a fix inside two sites' radii, or too loose to
trust, resolves to *no site* and is skipped, never guessed. A cross-site disagreement
escalates to a ``contradict`` shock only when (a) the geofence was unambiguous, (b) the
source has *earned* trust (its learned reliability clears the threshold), and (c)
emission is explicitly enabled — default-off until the geofence's precision is measured.

Layering (RFC 0012 pure/IO split):
- Pure: ``resolve_site`` (geofence + ambiguity abstain) / ``gps_strength`` — total
  functions over coordinates, unit-tested without a database.
- IO: ``harvest_photo_gps`` scans bin captures, records observations idempotently, and
  (gated) emits contradiction shocks.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import psycopg

from binkeeper.bin_inventory import (
    BIN_CONTRADICT_TRUST_THRESHOLD as _CONTRADICT_TRUST,
)
from binkeeper.bin_inventory import (
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    LocationObservation,
    bin_where,
    compute_source_reliabilities,
    observation_contradicts,
    record_event,
    record_observation,
    source_reliability,
)

# --- tunables (BINKEEPER_-prefixed, read at module top per RFC 0012) -----------


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


PHOTO_GPS_SOURCE: Final[str] = "photo_gps"
BIN_CAPTURE_KIND: Final[str] = "bin_capture"
# The default geofence radius for a site whose anchor omits one (mirrors build_payload.py).
BIN_GEOFENCE_DEFAULT_RADIUS_M: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_GEOFENCE_RADIUS_M", "150")
)
# A fix looser than this (accuracy radius, metres) is too uncertain to place a bin —
# abstain rather than corroborate a site the owner may not actually be standing in.
BIN_GEOFENCE_MAX_ACCURACY_M: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_GEOFENCE_MAX_ACCURACY_M", "100")
)
# GPS-accuracy → observation strength falloff: a fix at or under GOOD is full strength,
# degrading linearly to 0 at MAX_ACCURACY.
BIN_GPS_GOOD_ACCURACY_M: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_GPS_GOOD_ACCURACY_M", "15")
)
# Emitting contradiction shocks WRITES to the move ledger (and surfaces nudges), so it
# is default-off until the geofence's contradiction precision is measured (the load-
# bearing risk). Recording corroborative observations is always safe (never relocates).
BIN_HARVEST_EMIT_CONTRADICTIONS = _env_flag("BINKEEPER_BIN_HARVEST_EMIT_CONTRADICTIONS")
DEFAULT_SITES_FILE: Final[str] = os.environ.get(
    "BINKEEPER_BIN_SITES_FILE",
    str(Path(__file__).resolve().parents[2] / "scripts" / "bin-capture" / "sites.json"),
)


class BinHarvestError(Exception):
    """Domain root for harvester failures (RFC 0012 per-stage family)."""


# --- pure geofence + strength -----------------------------------------------


@dataclass(frozen=True)
class SiteAnchor:
    """A site's geofence centre + radius (None coords = not yet surveyed)."""

    lat: float | None
    lon: float | None
    radius_m: float | None = None


@dataclass(frozen=True)
class GeofenceResolution:
    """The outcome of resolving a GPS fix against the site geofences."""

    site: str | None
    ambiguous: bool
    too_loose: bool
    candidates: tuple[str, ...]

    @property
    def resolved(self) -> bool:
        """True iff a single unambiguous site was resolved."""
        return self.site is not None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (mirrors scripts/bin-capture/build_payload.py)."""
    radius_m = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(a))


def resolve_site(
    lat: float,
    lon: float,
    sites: Mapping[str, SiteAnchor],
    *,
    accuracy_m: float | None = None,
    default_radius_m: float = BIN_GEOFENCE_DEFAULT_RADIUS_M,
    max_accuracy_m: float = BIN_GEOFENCE_MAX_ACCURACY_M,
) -> GeofenceResolution:
    """Resolve a GPS fix to a single site, ABSTAINING on ambiguity. Pure and total.

    Unlike the capture-time helper (which picks the nearest containing site), the
    harvester refuses to guess: a fix inside two sites' radii is ``ambiguous`` and
    resolves to *no site* (the load-bearing geofence risk — a wrong corroboration
    trains the owner to ignore the surface). A fix looser than ``max_accuracy_m`` is
    ``too_loose`` and likewise abstains.
    """
    if accuracy_m is not None and accuracy_m > max_accuracy_m:
        return GeofenceResolution(site=None, ambiguous=False, too_loose=True, candidates=())
    containing: list[tuple[str, float]] = []
    for slug, anchor in sites.items():
        if anchor.lat is None or anchor.lon is None:
            continue
        radius = anchor.radius_m if anchor.radius_m is not None else default_radius_m
        dist = _haversine_m(lat, lon, anchor.lat, anchor.lon)
        if dist <= radius:
            containing.append((slug, dist))
    candidates = tuple(slug for slug, _ in sorted(containing, key=lambda item: item[1]))
    if not containing:
        return GeofenceResolution(site=None, ambiguous=False, too_loose=False, candidates=())
    if len(containing) > 1:
        return GeofenceResolution(site=None, ambiguous=True, too_loose=False, candidates=candidates)
    return GeofenceResolution(
        site=candidates[0], ambiguous=False, too_loose=False, candidates=candidates
    )


def gps_strength(
    accuracy_m: float | None,
    *,
    good_m: float = BIN_GPS_GOOD_ACCURACY_M,
    max_m: float = BIN_GEOFENCE_MAX_ACCURACY_M,
) -> float:
    """Map a GPS accuracy radius to an observation strength in [0,1]. Pure and total.

    A fix at or tighter than ``good_m`` is full strength; it degrades linearly to 0 at
    ``max_m``. An unknown accuracy gets a middling 0.7 (we still saw the bin there).
    """
    if accuracy_m is None:
        return 0.7
    if accuracy_m <= good_m:
        return 1.0
    if accuracy_m >= max_m or max_m <= good_m:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (accuracy_m - good_m) / (max_m - good_m)))


# --- sites config -----------------------------------------------------------


def load_sites(path: str | os.PathLike[str] = DEFAULT_SITES_FILE) -> dict[str, SiteAnchor]:
    """Load the geofence anchors from a sites.json file (missing file → empty config).

    Keys beginning with ``_`` (e.g. the example file's ``_README``) are ignored, so a
    not-yet-surveyed sites.json (all-null coords) simply resolves nothing rather than
    raising — the harvester is a safe no-op until the owner fills in coordinates.
    """
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
        lat = anchor.get("lat")
        lon = anchor.get("lon")
        radius = anchor.get("radius_m")
        sites[slug] = SiteAnchor(
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            radius_m=float(radius) if radius is not None else None,
        )
    return sites


# --- IO: the harvest worker -------------------------------------------------


@dataclass
class HarvestSummary:
    """The outcome of one harvest pass over the bin captures."""

    scanned: int = 0
    sites_configured: int = 0
    recorded: int = 0
    already: int = 0
    skipped_no_gps: int = 0
    skipped_no_site: int = 0
    skipped_ambiguous: int = 0
    skipped_too_loose: int = 0
    skipped_malformed: int = 0
    contradictions_emitted: int = 0
    contradictions_suppressed: int = 0
    emit_contradictions: bool = False

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the summary."""
        return {
            "scanned": self.scanned,
            "sites_configured": self.sites_configured,
            "recorded": self.recorded,
            "already": self.already,
            "skipped_no_gps": self.skipped_no_gps,
            "skipped_no_site": self.skipped_no_site,
            "skipped_ambiguous": self.skipped_ambiguous,
            "skipped_too_loose": self.skipped_too_loose,
            "skipped_malformed": self.skipped_malformed,
            "contradictions_emitted": self.contradictions_emitted,
            "contradictions_suppressed": self.contradictions_suppressed,
            "emit_contradictions": self.emit_contradictions,
        }


def _parse_gps(meta: Mapping[str, Any]) -> tuple[float, float, float | None] | None:
    """Extract (lat, lon, accuracy_m) from a bin_capture metadata object, or None."""
    gps = meta.get("gps")
    if not isinstance(gps, dict):
        return None
    lat, lon = gps.get("lat"), gps.get("lon")
    if lat is None or lon is None:
        return None
    try:
        accuracy = gps.get("accuracy_m")
        return float(lat), float(lon), (float(accuracy) if accuracy is not None else None)
    except (TypeError, ValueError):
        return None


def _observed_at(meta: Mapping[str, Any], fallback: datetime) -> datetime:
    """The capture's observation time — metadata.captured_at if parseable, else fallback."""
    captured = meta.get("captured_at")
    if isinstance(captured, str):
        try:
            return datetime.fromisoformat(captured)
        except ValueError:
            return fallback
    return fallback


def harvest_photo_gps(
    conn: psycopg.Connection,
    *,
    sites: Mapping[str, SiteAnchor] | None = None,
    now: datetime | None = None,
    emit_contradictions: bool | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> HarvestSummary:
    """Backfill corroborative observations from the GPS in every bin capture.

    For each ``bin_capture`` with a usable GPS fix, resolves a site server-side
    (abstaining on geofence ambiguity / a too-loose fix) and records an idempotent
    ``photo_gps`` observation keyed on the capture. When ``emit_contradictions`` is
    enabled AND the source has earned trust, a cross-site disagreement also writes a
    gated ``contradict`` shock to the move ledger (never a relocation). Idempotent: a
    second pass dedupes on the capture identity.
    """
    resolved_sites = dict(sites) if sites is not None else load_sites()
    do_contradict = (
        BIN_HARVEST_EMIT_CONTRADICTIONS if emit_contradictions is None else emit_contradictions
    )
    when = now or datetime.now(UTC)
    summary = HarvestSummary(
        sites_configured=sum(1 for a in resolved_sites.values() if a.lat is not None),
        emit_contradictions=do_contradict,
    )
    # Reliability is a pre-existing track record; compute it once for the run's gating.
    reliabilities = compute_source_reliabilities(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    photo_gps_reliability = reliabilities.get(
        PHOTO_GPS_SOURCE, source_reliability(PHOTO_GPS_SOURCE, hits=0, misses=0)
    )

    rows = conn.execute(
        """
        SELECT external_id, raw_payload, observed_at, imported_at
        FROM captures
        WHERE raw_payload->'metadata'->>'kind' = %s
        ORDER BY imported_at
        """,
        (BIN_CAPTURE_KIND,),
    ).fetchall()

    for external_id, raw_payload, observed_at, imported_at in rows:
        summary.scanned += 1
        meta = raw_payload.get("metadata") if isinstance(raw_payload, dict) else None
        if not isinstance(meta, dict):
            summary.skipped_malformed += 1
            continue
        bin_code = meta.get("bin_code")
        if not isinstance(bin_code, str) or not bin_code.strip():
            summary.skipped_malformed += 1
            continue
        fix = _parse_gps(meta)
        if fix is None:
            summary.skipped_no_gps += 1
            continue
        lat, lon, accuracy = fix
        resolution = resolve_site(lat, lon, resolved_sites, accuracy_m=accuracy)
        if not resolution.resolved:
            if resolution.ambiguous:
                summary.skipped_ambiguous += 1
            elif resolution.too_loose:
                summary.skipped_too_loose += 1
            else:
                summary.skipped_no_site += 1
            continue
        observed = _observed_at(meta, observed_at or imported_at or when)
        result = record_observation(
            conn,
            bin_code=bin_code,
            observed_site=resolution.site,  # type: ignore[arg-type]
            source_kind=PHOTO_GPS_SOURCE,
            observed_at=observed,
            observation_strength=gps_strength(accuracy),
            evidence_ref=external_id,
            idempotency_key=f"photo-gps:{external_id}",
            metadata={
                "lat": lat,
                "lon": lon,
                "accuracy_m": accuracy,
                "geofence_candidates": list(resolution.candidates),
            },
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
        if result.already_existed:
            summary.already += 1
        else:
            summary.recorded += 1

        # A cross-site disagreement: the geofence (unambiguous, by construction here)
        # places the bin at a different site than the move ledger. Escalate to a shock
        # only when emission is enabled AND the source has earned trust.
        location = bin_where(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
        observation = LocationObservation(
            seq=0,
            bin_code=bin_code,
            observed_site=resolution.site,  # type: ignore[arg-type]
            source_kind=PHOTO_GPS_SOURCE,
            observed_at=observed,
            observation_strength=gps_strength(accuracy),
        )
        if observation_contradicts(observation, location):
            if do_contradict and photo_gps_reliability >= _CONTRADICT_TRUST:
                shock = record_event(
                    conn,
                    event_kind="contradict",
                    bin_code=bin_code,
                    occurred_at=observed,
                    source_label=PHOTO_GPS_SOURCE,
                    idempotency_key=f"photo-gps-contradict:{external_id}",
                    metadata={
                        "observed_site": resolution.site,
                        "ledger_site": location.site,
                        "evidence_ref": external_id,
                        "reliability": photo_gps_reliability,
                    },
                    tenant_id=tenant_id,
                    corpus_id=corpus_id,
                )
                if not shock.already_existed:
                    summary.contradictions_emitted += 1
            else:
                summary.contradictions_suppressed += 1

    return summary
