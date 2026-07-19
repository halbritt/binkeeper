"""RFC 0088 T2 — physical bin inventory: append-only move/trip ledger.

A bin's current location is a *fold* over its move events (``bin_trip_events``,
migration 046), never an editable cell — so a stale location is structurally
impossible. A trip models a truck/trailer as a transient container with a
load/arrive checksum, so a bin cannot silently vanish between two sites
(the owner's stated fear). See ``docs/rfcs/0088-physical-bin-inventory.md``.

Layering (RFC 0012 pure/IO split):
- Pure: ``validate_event`` / ``fold_bin_location`` / ``trip_checksum`` —
  total functions over in-memory events, unit-tested without a database.
- IO: ``record_event`` (idempotent append, RFC 0001) / ``load_bin_events`` /
  ``load_trip_events`` / ``bin_where`` / ``trip_status`` / ``arrive_all``.

Capture stays low-friction: a bare ``place`` needs only (bin_code, site); the
trip checksum is opt-in and only for deliberate moves.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final, Literal

import psycopg
from psycopg.types.json import Jsonb

# --- tunables (BINKEEPER_-prefixed, read at module top per RFC 0012) -----------

DEFAULT_BIN_TENANT_ID: Final[str] = os.environ.get("BINKEEPER_BIN_TENANT_ID", "personal")
DEFAULT_BIN_CORPUS_ID: Final[str] = os.environ.get("BINKEEPER_BIN_CORPUS_ID", "personal")
DEFAULT_BIN_SOURCE_LABEL: Final[str] = "manual"
TRIP_EVENT_SCHEMA_VERSION: Final[str] = "bin_trip_event.v2"

EventKind = Literal[
    "place", "open", "load", "arrive", "close", "confirm", "contradict", "fetch", "not_found"
]
EVENT_KINDS: Final[tuple[EventKind, ...]] = (
    "place",
    "open",
    "load",
    "arrive",
    "close",
    "confirm",
    "contradict",
    "fetch",
    "not_found",
)
# Events that assert where a bin currently rests (the fold's location-bearing set).
# A `fetch` is a successful retrieval: the owner had the bin in hand at that site,
# so it re-confirms location exactly as `confirm` does. A `not_found` asserts where
# the bin is NOT and is deliberately excluded — it shocks confidence instead.
LOCATION_EVENT_KINDS: Final[tuple[EventKind, ...]] = ("place", "load", "arrive", "confirm", "fetch")
# Events that count as an actual physical MOVE — used to learn a per-bin half-life;
# a `confirm` re-verifies a bin in place and is deliberately excluded.
MOVE_EVENT_KINDS: Final[tuple[EventKind, ...]] = ("place", "load", "arrive")
LocationStatus = Literal["at_site", "in_transit", "unknown"]

# --- T3 belief tunables (RFC 0088 §4; default-off serving) ------------------


def _env_flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


# Gates whether retrieval/serving ABSTAINS on a stale location belief. Off =>
# bin_where() stays byte-identical; the bin_belief() instrument is always available
# for calibration (instrument-first, like BINKEEPER_READ_PATH_ABSTAIN_ENABLED).
BIN_BELIEF_SERVING_ENABLED = _env_flag("BINKEEPER_BIN_BELIEF_ENABLED")
# Confidence floor M_loc in [0,1]: a belief below it abstains. A frozen constant for
# now; a calibrated, corpus-keyed floor + a D098 freeze precede the default-on flip.
BIN_LOCATION_FLOOR = float(os.environ.get("BINKEEPER_BIN_M_LOC", "0.5"))
# Half-life (days) for bins with too few moves to learn their own cadence.
BIN_DEFAULT_HALF_LIFE_DAYS = float(os.environ.get("BINKEEPER_BIN_DEFAULT_HALF_LIFE_DAYS", "120"))
# Confidence drop per post-placement `contradict` event (a shock, not a clock tick).
BIN_CONTRADICTION_SHOCK = float(os.environ.get("BINKEEPER_BIN_CONTRADICTION_SHOCK", "0.6"))
BIN_BELIEF_PROJECTOR_VERSION = os.environ.get(
    "BINKEEPER_BIN_BELIEF_PROJECTOR_VERSION", "loc-decay-v1"
)

# --- T3b passive-harvest tunables (RFC 0088 §4; corroborative-only) ---------

# Per-source Beta prior (alpha, beta) for the learned reliability posterior. A source
# starts trusted at alpha/(alpha+beta) and is updated by how often its observed_site
# agreed with the move ledger's as-of ground truth. Photo/GPS dwell are fairly reliable
# a priori; peripheral OCR is middling; transcript deixis is noisy. Unknown sources fall
# back to a neutral 0.5 prior, so a new source must earn trust before it can lift a
# bin's confidence or emit a contradiction.
BIN_SOURCE_PRIORS: Final[dict[str, tuple[float, float]]] = {
    "photo_gps": (4.0, 1.0),
    "gps_dwell": (4.0, 1.0),
    "manual": (9.0, 1.0),
    "peripheral_ocr": (3.0, 2.0),
    "transcript_deixis": (1.0, 2.0),
}
BIN_DEFAULT_SOURCE_PRIOR: Final[tuple[float, float]] = (1.0, 1.0)
# Reliability a source must reach before a cross-site disagreement is allowed to emit a
# `contradict` shock (the load-bearing geofence risk: a flaky source must never train the
# owner to ignore the surface). The harvester also requires an unambiguous geofence.
BIN_CONTRADICT_TRUST_THRESHOLD = float(os.environ.get("BINKEEPER_BIN_CONTRADICT_TRUST", "0.6"))
OBSERVATION_SCHEMA_VERSION: Final[str] = "location_observation.v1"


class BinInventoryError(Exception):
    """Domain root for bin-inventory failures (RFC 0012 per-stage family)."""


# --- pure dataclasses -------------------------------------------------------


@dataclass(frozen=True)
class TripEvent:
    """One append-only move event, as the fold sees it."""

    seq: int
    event_kind: EventKind
    occurred_at: datetime
    trip_id: str | None = None
    bin_code: str | None = None
    from_site: str | None = None
    to_site: str | None = None
    site: str | None = None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one event."""
        return {
            "seq": self.seq,
            "event_kind": self.event_kind,
            "occurred_at": self.occurred_at.isoformat(),
            "trip_id": self.trip_id,
            "bin_code": self.bin_code,
            "from_site": self.from_site,
            "to_site": self.to_site,
            "site": self.site,
        }


@dataclass(frozen=True)
class BinLocation:
    """The folded current location of one bin."""

    bin_code: str
    status: LocationStatus
    site: str | None
    trip_id: str | None
    last_event_seq: int | None
    last_event_at: datetime | None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a location answer."""
        return {
            "bin_code": self.bin_code,
            "status": self.status,
            "site": self.site,
            "trip_id": self.trip_id,
            "last_event_seq": self.last_event_seq,
            "last_event_at": self.last_event_at.isoformat() if self.last_event_at else None,
        }


@dataclass(frozen=True)
class TripChecksum:
    """The reconciliation state of one trip."""

    trip_id: str
    from_site: str | None
    to_site: str | None
    is_open: bool
    is_closed: bool
    loaded: tuple[str, ...]
    arrived: tuple[str, ...]
    unaccounted: tuple[str, ...]
    reconciled: bool = field(default=False)

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a trip checksum."""
        return {
            "trip_id": self.trip_id,
            "from_site": self.from_site,
            "to_site": self.to_site,
            "is_open": self.is_open,
            "is_closed": self.is_closed,
            "loaded": list(self.loaded),
            "arrived": list(self.arrived),
            "unaccounted": list(self.unaccounted),
            "reconciled": self.reconciled,
        }


@dataclass(frozen=True)
class RecordResult:
    """The outcome of an idempotent append."""

    external_id: str
    event_id: str
    seq: int
    already_existed: bool

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {
            "external_id": self.external_id,
            "event_id": self.event_id,
            "seq": self.seq,
            "already_existed": self.already_existed,
        }


# --- pure validation & folds ------------------------------------------------


def validate_event(
    *,
    event_kind: str,
    trip_id: str | None,
    bin_code: str | None,
    site: str | None,
) -> EventKind:
    """Validate a move event's shape; return the normalized kind or raise.

    Mirrors the migration's CHECK constraints so the worker fails fast with a
    clear message rather than surfacing a raw constraint violation.
    """
    if event_kind not in EVENT_KINDS:
        allowed = ", ".join(EVENT_KINDS)
        raise BinInventoryError(f"unknown event_kind {event_kind!r}; allowed: {allowed}")
    kind: EventKind = event_kind  # type: ignore[assignment]
    bin_required = kind not in ("open", "close")
    if bin_required and not (bin_code and bin_code.strip()):
        raise BinInventoryError(f"event_kind {kind!r} requires a bin_code")
    if not bin_required and bin_code:
        raise BinInventoryError(f"event_kind {kind!r} must not carry a bin_code")
    trip_free = ("place", "confirm", "contradict", "fetch", "not_found")
    if kind not in trip_free and not (trip_id and trip_id.strip()):
        raise BinInventoryError(f"event_kind {kind!r} requires a trip_id")
    if kind in ("place", "arrive", "confirm", "fetch", "not_found") and not (site and site.strip()):
        raise BinInventoryError(f"event_kind {kind!r} requires a site")
    return kind


def fold_bin_location(bin_code: str, events: Sequence[TripEvent]) -> BinLocation:
    """Fold a bin's events into its current location (latest by seq wins).

    place/arrive -> at that site; load -> in_transit on that trip. Events that
    do not move this bin are ignored. Pure and total.
    """
    relevant = [
        event
        for event in events
        if event.bin_code == bin_code and event.event_kind in LOCATION_EVENT_KINDS
    ]
    if not relevant:
        return BinLocation(bin_code, "unknown", None, None, None, None)
    latest = max(relevant, key=lambda event: event.seq)
    if latest.event_kind == "load":
        return BinLocation(
            bin_code, "in_transit", None, latest.trip_id, latest.seq, latest.occurred_at
        )
    return BinLocation(bin_code, "at_site", latest.site, None, latest.seq, latest.occurred_at)


def trip_checksum(trip_id: str, events: Sequence[TripEvent]) -> TripChecksum:
    """Reconcile a trip: which loaded bins have not yet arrived. Pure and total."""
    own = [event for event in events if event.trip_id == trip_id]
    is_open = any(event.event_kind == "open" for event in own)
    is_closed = any(event.event_kind == "close" for event in own)
    from_site = next((event.from_site for event in own if event.event_kind == "open"), None)
    to_site = next((event.to_site for event in own if event.event_kind == "open"), None)
    loaded = _ordered_unique(
        event.bin_code for event in own if event.event_kind == "load" and event.bin_code
    )
    arrived = _ordered_unique(
        event.bin_code for event in own if event.event_kind == "arrive" and event.bin_code
    )
    arrived_set = set(arrived)
    unaccounted = tuple(code for code in loaded if code not in arrived_set)
    return TripChecksum(
        trip_id=trip_id,
        from_site=from_site,
        to_site=to_site,
        is_open=is_open and not is_closed,
        is_closed=is_closed,
        loaded=loaded,
        arrived=arrived,
        unaccounted=unaccounted,
        reconciled=not unaccounted,
    )


def _ordered_unique(values: object) -> tuple[str, ...]:
    """Return the values in first-seen order with duplicates removed."""
    seen: dict[str, None] = {}
    for value in values:  # type: ignore[attr-defined]
        if isinstance(value, str):
            seen.setdefault(value, None)
    return tuple(seen)


def _canonical_json(payload: dict[str, object]) -> str:
    """Deterministic JSON for hashing (sorted keys, compact)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_external_id(
    *,
    event_kind: str,
    trip_id: str | None,
    bin_code: str | None,
    site: str | None,
    occurred_at: datetime,
    idempotency_key: str | None,
    tenant_id: str,
    corpus_id: str,
) -> str:
    """Derive the idempotency handle for a move event.

    An explicit ``idempotency_key`` is the dedupe anchor (a retried scan reuses
    it); otherwise the handle is the content hash of the event's core fields, so
    a byte-identical replay still dedupes.
    """
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": tenant_id,
                "corpus_id": corpus_id,
                "event_kind": event_kind,
                "trip_id": trip_id,
                "bin_code": bin_code,
                "site": site,
                "occurred_at": occurred_at.isoformat(),
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"trip:{digest[:40]}"


# --- pure T3 location belief (decay + abstain), RFC 0088 §4 -----------------


def _median(values: list[float]) -> float:
    """Median of a non-empty list."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


@dataclass(frozen=True)
class BinBelief:
    """A bin's folded location plus a time-decayed confidence and an abstain verdict."""

    bin_code: str
    location: BinLocation
    confidence: float
    half_life_days: float
    floor: float
    abstained: bool
    age_days: float | None
    corroborated_by_source: str | None = None
    corroboration_age_days: float | None = None
    observation_count: int = 0

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a belief answer."""
        return {
            "bin_code": self.bin_code,
            "location": self.location.to_json(),
            "confidence": round(self.confidence, 4),
            "half_life_days": round(self.half_life_days, 2),
            "floor": self.floor,
            "abstained": self.abstained,
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
            "corroborated_by_source": self.corroborated_by_source,
            "corroboration_age_days": (
                round(self.corroboration_age_days, 2)
                if self.corroboration_age_days is not None
                else None
            ),
            "observation_count": self.observation_count,
            "serving_enabled": BIN_BELIEF_SERVING_ENABLED,
        }


def bin_half_life_days(
    events: Sequence[TripEvent], *, default_days: float = BIN_DEFAULT_HALF_LIFE_DAYS
) -> float:
    """Learn a per-bin half-life from its inter-MOVE spacing. Pure and total.

    The median gap (in days) between actual moves (place/load/arrive — a `confirm`
    re-verifies in place and is excluded). A bin reshuffled monthly yields ~30d; a
    bin untouched for years yields a long, near-flat half-life. Fewer than two
    moves falls back to ``default_days``.
    """
    moves = sorted(
        (event for event in events if event.event_kind in MOVE_EVENT_KINDS),
        key=lambda event: event.seq,
    )
    if len(moves) < 2:
        return default_days
    gaps = [
        (moves[i].occurred_at - moves[i - 1].occurred_at).total_seconds() / 86400.0
        for i in range(1, len(moves))
    ]
    gaps = [gap for gap in gaps if gap > 0]
    return _median(gaps) if gaps else default_days


def decayed_confidence(
    last_event_at: datetime, half_life_days: float, now: datetime, *, shock: float = 0.0
) -> float:
    """Exponential-decay confidence in [0,1] from 1.0 at ``last_event_at``.

    Confidence halves every ``half_life_days``; ``shock`` (e.g. a contradiction)
    subtracts a flat amount. Pure and total; clamped to [0,1].
    """
    age_days = (now - last_event_at).total_seconds() / 86400.0
    if age_days <= 0:
        base = 1.0
    elif half_life_days <= 0:
        base = 0.0
    else:
        base = 0.5 ** (age_days / half_life_days)
    return max(0.0, min(1.0, base - shock))


def _contradiction_shock(events: Sequence[TripEvent], *, after_seq: int) -> float:
    """Total confidence shock from post-placement contradiction evidence.

    Both `contradict` (a harvester's cross-site disagreement) and `not_found`
    (the owner looked where the fold claims and the bin was not there) count:
    each is direct evidence against the current location claim, applied after
    the last location-bearing event.
    """
    contradictions = sum(
        1
        for event in events
        if event.event_kind in ("contradict", "not_found") and event.seq > after_seq
    )
    return contradictions * BIN_CONTRADICTION_SHOCK


def compute_belief(
    bin_code: str,
    events: Sequence[TripEvent],
    *,
    now: datetime,
    floor: float = BIN_LOCATION_FLOOR,
    default_half_life_days: float = BIN_DEFAULT_HALF_LIFE_DAYS,
    observations: Sequence[LocationObservation] = (),
    source_reliability_by_kind: Mapping[str, float] | None = None,
) -> BinBelief:
    """Fold location + time-decayed confidence + the RFC 0087 abstain gate. Pure.

    A bin with no location-bearing event is unknown and always abstains. Otherwise
    confidence decays from the latest place/load/arrive/confirm/fetch by a per-bin
    half-life, minus any post-placement contradiction shock (`contradict` or
    `not_found`), and the (reused) absolute gate abstains when it falls below
    ``floor``.

    T3b: ``observations`` that CORROBORATE the folded site (same site) can *refresh*
    confidence — an observation's contribution is the product of its source's learned
    reliability, its own strength, and the decay from when it was observed, and the
    belief takes the MAX over the move clock and the best corroboration. A cross-site
    observation is
    ignored here (it never relocates the bin; the harvester escalates it to a
    contradiction). So a noisy harvester can at most falsely refresh a timestamp,
    never assert a wrong site, and a low-reliability source can only lift confidence
    up to its reliability.
    """
    location = fold_bin_location(bin_code, events)
    if location.last_event_at is None or location.last_event_seq is None:
        return BinBelief(
            bin_code=bin_code,
            location=location,
            confidence=0.0,
            half_life_days=default_half_life_days,
            floor=floor,
            abstained=True,
            age_days=None,
        )
    from binkeeper.abstain import absolute_gate

    half_life = bin_half_life_days(events, default_days=default_half_life_days)
    shock = _contradiction_shock(events, after_seq=location.last_event_seq)
    move_confidence = decayed_confidence(location.last_event_at, half_life, now, shock=shock)

    reliabilities = source_reliability_by_kind or {}
    best_obs_confidence = 0.0
    best_obs: LocationObservation | None = None
    corroboration_count = 0
    if location.status == "at_site" and location.site is not None:
        for obs in observations:
            if obs.bin_code != bin_code or obs.observed_site != location.site:
                continue  # corroborative-only: only same-site observations refresh
            corroboration_count += 1
            reliability = reliabilities.get(obs.source_kind, _prior_mean(obs.source_kind))
            obs_confidence = (
                reliability
                * obs.observation_strength
                * decayed_confidence(obs.observed_at, half_life, now)
            )
            if obs_confidence > best_obs_confidence:
                best_obs_confidence = obs_confidence
                best_obs = obs

    confidence = max(move_confidence, best_obs_confidence)
    decision = absolute_gate(confidence, floor)
    age_days = (now - location.last_event_at).total_seconds() / 86400.0
    held_by_obs = best_obs is not None and best_obs_confidence > move_confidence
    return BinBelief(
        bin_code=bin_code,
        location=location,
        confidence=confidence,
        half_life_days=half_life,
        floor=floor,
        abstained=decision.abstained,
        age_days=age_days,
        corroborated_by_source=best_obs.source_kind if held_by_obs and best_obs else None,
        corroboration_age_days=(
            (now - best_obs.observed_at).total_seconds() / 86400.0
            if held_by_obs and best_obs
            else None
        ),
        observation_count=corroboration_count,
    )


# --- pure T3b: passive-harvest observations (corroborate, never relocate) ----


@dataclass(frozen=True)
class LocationObservation:
    """One corroborative location signal harvested from a free, unrelated source."""

    seq: int
    bin_code: str
    observed_site: str
    source_kind: str
    observed_at: datetime
    observation_strength: float = 1.0
    evidence_ref: str | None = None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one observation."""
        return {
            "seq": self.seq,
            "bin_code": self.bin_code,
            "observed_site": self.observed_site,
            "source_kind": self.source_kind,
            "observed_at": self.observed_at.isoformat(),
            "observation_strength": self.observation_strength,
            "evidence_ref": self.evidence_ref,
        }


def _prior_mean(source_kind: str) -> float:
    """The prior reliability of a source before any outcome is observed."""
    alpha, beta = BIN_SOURCE_PRIORS.get(source_kind, BIN_DEFAULT_SOURCE_PRIOR)
    return alpha / (alpha + beta)


def as_of_site(bin_code: str, events: Sequence[TripEvent], at: datetime) -> str | None:
    """The site the MOVE ledger places ``bin_code`` at as of ``at`` (ground truth). Pure.

    Folds only events that occurred at or before ``at`` (the deliberate move ledger is
    authoritative); returns the resting site, or None if the bin was unknown or in
    transit at that time. Used to adjudicate an observation against what the ledger knew.
    """
    past = [event for event in events if event.occurred_at <= at]
    location = fold_bin_location(bin_code, past)
    return location.site if location.status == "at_site" else None


def adjudicate_observation(
    observation: LocationObservation, move_events: Sequence[TripEvent]
) -> bool | None:
    """Did the observation agree with the move ledger's as-of-observed_at site? Pure.

    True (hit) / False (miss) / None (unadjudicable — the ledger had no resting site for
    the bin at that time). The move ledger is the arbiter: a source's reliability is
    learned from how often it is contradicted by a real move (RFC 0088 §4 T3b).
    """
    truth = as_of_site(observation.bin_code, move_events, observation.observed_at)
    if truth is None:
        return None
    return observation.observed_site == truth


def source_reliability(source_kind: str, *, hits: int, misses: int) -> float:
    """Beta-posterior reliability in (0,1) from a per-source prior + adjudicated outcomes."""
    alpha, beta = BIN_SOURCE_PRIORS.get(source_kind, BIN_DEFAULT_SOURCE_PRIOR)
    return (alpha + hits) / (alpha + beta + hits + misses)


def observation_contradicts(observation: LocationObservation, location: BinLocation) -> bool:
    """True iff the observation places a sited bin at a DIFFERENT site than the fold. Pure.

    A disagreement the harvester may escalate to a `contradict` shock (gated by source
    reliability + an unambiguous geofence); it never relocates the bin (the T3b invariant).
    """
    return (
        location.status == "at_site"
        and location.site is not None
        and observation.observed_site != location.site
    )


# --- IO -----------------------------------------------------------------------

_SELECT_COLUMNS = "seq, event_kind, occurred_at, trip_id, bin_code, from_site, to_site, site"


def _row_to_event(row: tuple[object, ...]) -> TripEvent:
    """Map a SELECT row (in _SELECT_COLUMNS order) to a TripEvent."""
    return TripEvent(
        seq=int(row[0]),  # type: ignore[arg-type]
        event_kind=row[1],  # type: ignore[arg-type]
        occurred_at=row[2],  # type: ignore[arg-type]
        trip_id=row[3],  # type: ignore[arg-type]
        bin_code=row[4],  # type: ignore[arg-type]
        from_site=row[5],  # type: ignore[arg-type]
        to_site=row[6],  # type: ignore[arg-type]
        site=row[7],  # type: ignore[arg-type]
    )


def record_event(
    conn: psycopg.Connection,
    *,
    event_kind: str,
    trip_id: str | None = None,
    bin_code: str | None = None,
    from_site: str | None = None,
    to_site: str | None = None,
    site: str | None = None,
    occurred_at: datetime | None = None,
    source_label: str = DEFAULT_BIN_SOURCE_LABEL,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> RecordResult:
    """Append one move event idempotently; return its handle (RFC 0001)."""
    kind = validate_event(event_kind=event_kind, trip_id=trip_id, bin_code=bin_code, site=site)
    when = occurred_at or datetime.now(UTC)
    external_id = event_external_id(
        event_kind=kind,
        trip_id=trip_id,
        bin_code=bin_code,
        site=site,
        occurred_at=when,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    raw_payload: dict[str, object] = {
        "schema_version": TRIP_EVENT_SCHEMA_VERSION,
        "metadata": metadata or {},
        "idempotency_key": idempotency_key,
    }
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO bin_trip_events (
                tenant_id, corpus_id, external_id, event_kind,
                trip_id, bin_code, from_site, to_site, site,
                occurred_at, source_label, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                kind,
                trip_id,
                bin_code,
                from_site,
                to_site,
                site,
                when,
                source_label,
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return RecordResult(external_id, str(inserted[0]), int(inserted[1]), False)
        existing = conn.execute(
            """
            SELECT id::text, seq FROM bin_trip_events
            WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
            """,
            (tenant_id, corpus_id, external_id),
        ).fetchone()
    if existing is None:  # pragma: no cover — conflict without a row is impossible
        raise BinInventoryError("idempotent insert conflicted but no prior row was found")
    return RecordResult(external_id, str(existing[0]), int(existing[1]), True)


def load_bin_events(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[TripEvent]:
    """Load one bin's move events in seq order."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM bin_trip_events
        WHERE tenant_id = %s AND corpus_id = %s AND bin_code = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id, bin_code),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def load_trip_events(
    conn: psycopg.Connection,
    trip_id: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[TripEvent]:
    """Load one trip's events in seq order."""
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLUMNS} FROM bin_trip_events
        WHERE tenant_id = %s AND corpus_id = %s AND trip_id = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id, trip_id),
    ).fetchall()
    return [_row_to_event(row) for row in rows]


def bin_where(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> BinLocation:
    """Resolve a bin's current location by folding its events."""
    events = load_bin_events(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
    return fold_bin_location(bin_code, events)


def trip_status(
    conn: psycopg.Connection,
    trip_id: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> TripChecksum:
    """Reconcile a trip from its events."""
    events = load_trip_events(conn, trip_id, tenant_id=tenant_id, corpus_id=corpus_id)
    return trip_checksum(trip_id, events)


def arrive_all(
    conn: psycopg.Connection,
    trip_id: str,
    *,
    site: str | None = None,
    occurred_at: datetime | None = None,
    source_label: str = DEFAULT_BIN_SOURCE_LABEL,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[RecordResult]:
    """Arrive every still-loaded bin on a trip in one call (clear-the-trip).

    The low-friction end-of-move action: defaults the arrival site to the trip's
    ``to_site``. Idempotent per (trip, bin, site) so re-running is safe.
    """
    checksum = trip_status(conn, trip_id, tenant_id=tenant_id, corpus_id=corpus_id)
    arrival_site = site or checksum.to_site
    if not arrival_site:
        raise BinInventoryError(
            f"trip {trip_id!r} has no to_site; pass an explicit site to arrive_all"
        )
    results: list[RecordResult] = []
    for code in checksum.unaccounted:
        results.append(
            record_event(
                conn,
                event_kind="arrive",
                trip_id=trip_id,
                bin_code=code,
                site=arrival_site,
                occurred_at=occurred_at,
                source_label=source_label,
                idempotency_key=f"arrive-all:{trip_id}:{code}:{arrival_site}",
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
        )
    return results


def bin_belief(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    now: datetime | None = None,
    floor: float = BIN_LOCATION_FLOOR,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> BinBelief:
    """Resolve a bin's location belief — folded location + decayed confidence + abstain.

    T3b: corroborating ``location_observation`` rows can refresh confidence, weighted by
    each source's learned reliability (a read-time projection over the whole corpus).
    """
    events = load_bin_events(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
    observations = load_bin_observations(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
    reliabilities = (
        compute_source_reliabilities(conn, tenant_id=tenant_id, corpus_id=corpus_id)
        if observations
        else None
    )
    return compute_belief(
        bin_code,
        events,
        now=now or datetime.now(UTC),
        floor=floor,
        observations=observations,
        source_reliability_by_kind=reliabilities,
    )


# --- IO T3b: passive-harvest observation stream -----------------------------

_OBS_COLUMNS = (
    "seq, bin_code, observed_site, source_kind, observed_at, observation_strength, evidence_ref"
)


def observation_external_id(
    *,
    bin_code: str,
    observed_site: str,
    source_kind: str,
    observed_at: datetime,
    evidence_ref: str | None,
    idempotency_key: str | None,
    tenant_id: str,
    corpus_id: str,
) -> str:
    """Derive the idempotency handle for an observation (mirrors ``event_external_id``).

    An explicit ``idempotency_key`` is the dedupe anchor; otherwise the handle is the
    content hash of the observation's core fields, so re-harvesting the same evidence
    (same capture, same resolved site) dedupes rather than double-counting.
    """
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = _canonical_json(
            {
                "tenant_id": tenant_id,
                "corpus_id": corpus_id,
                "bin_code": bin_code,
                "observed_site": observed_site,
                "source_kind": source_kind,
                "observed_at": observed_at.isoformat(),
                "evidence_ref": evidence_ref,
            }
        )
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()
    return f"obs:{digest[:40]}"


def _row_to_observation(row: tuple[object, ...]) -> LocationObservation:
    """Map a SELECT row (in _OBS_COLUMNS order) to a LocationObservation."""
    return LocationObservation(
        seq=int(row[0]),  # type: ignore[arg-type]
        bin_code=row[1],  # type: ignore[arg-type]
        observed_site=row[2],  # type: ignore[arg-type]
        source_kind=row[3],  # type: ignore[arg-type]
        observed_at=row[4],  # type: ignore[arg-type]
        observation_strength=float(row[5]),  # type: ignore[arg-type]
        evidence_ref=row[6],  # type: ignore[arg-type]
    )


def record_observation(
    conn: psycopg.Connection,
    *,
    bin_code: str,
    observed_site: str,
    source_kind: str,
    observed_at: datetime | None = None,
    observation_strength: float = 1.0,
    evidence_ref: str | None = None,
    idempotency_key: str | None = None,
    metadata: dict[str, object] | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> RecordResult:
    """Append one corroborative location observation idempotently (RFC 0001).

    Validates that the observation is well-formed (a bin, a resolved site, a source);
    an observation with no resolved site is the harvester's responsibility to drop
    (abstain on geofence ambiguity) — it never reaches here with an empty site.
    """
    if not (bin_code and bin_code.strip()):
        raise BinInventoryError("record_observation requires a bin_code")
    if not (observed_site and observed_site.strip()):
        raise BinInventoryError("record_observation requires an observed_site")
    if not (source_kind and source_kind.strip()):
        raise BinInventoryError("record_observation requires a source_kind")
    if not 0.0 <= observation_strength <= 1.0:
        raise BinInventoryError("observation_strength must be in [0, 1]")
    when = observed_at or datetime.now(UTC)
    external_id = observation_external_id(
        bin_code=bin_code,
        observed_site=observed_site,
        source_kind=source_kind,
        observed_at=when,
        evidence_ref=evidence_ref,
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    raw_payload: dict[str, object] = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "metadata": metadata or {},
        "idempotency_key": idempotency_key,
    }
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO location_observation (
                tenant_id, corpus_id, external_id, bin_code, observed_site,
                source_kind, observation_strength, evidence_ref, observed_at, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                bin_code,
                observed_site,
                source_kind,
                observation_strength,
                evidence_ref,
                when,
                Jsonb(raw_payload),
            ),
        ).fetchone()
        if inserted is not None:
            return RecordResult(external_id, str(inserted[0]), int(inserted[1]), False)
        existing = conn.execute(
            """
            SELECT id::text, seq FROM location_observation
            WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
            """,
            (tenant_id, corpus_id, external_id),
        ).fetchone()
    if existing is None:  # pragma: no cover — conflict without a row is impossible
        raise BinInventoryError("idempotent insert conflicted but no prior row was found")
    return RecordResult(external_id, str(existing[0]), int(existing[1]), True)


def load_bin_observations(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[LocationObservation]:
    """Load one bin's observations in seq order."""
    rows = conn.execute(
        f"""
        SELECT {_OBS_COLUMNS} FROM location_observation
        WHERE tenant_id = %s AND corpus_id = %s AND bin_code = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id, bin_code),
    ).fetchall()
    return [_row_to_observation(row) for row in rows]


def load_observations(
    conn: psycopg.Connection,
    *,
    source_kind: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[LocationObservation]:
    """Load observations (optionally one source) in seq order — for the reliability fold."""
    if source_kind is not None:
        rows = conn.execute(
            f"""
            SELECT {_OBS_COLUMNS} FROM location_observation
            WHERE tenant_id = %s AND corpus_id = %s AND source_kind = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id, source_kind),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT {_OBS_COLUMNS} FROM location_observation
            WHERE tenant_id = %s AND corpus_id = %s
            ORDER BY seq
            """,
            (tenant_id, corpus_id),
        ).fetchall()
    return [_row_to_observation(row) for row in rows]


def compute_source_reliabilities(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> dict[str, float]:
    """Project each source's reliability from how often it agreed with the move ledger.

    A read-time projection (no stored weight): every observation is adjudicated against
    the move ledger's as-of-observed_at ground truth, and the Beta posterior per source
    folds the per-source prior with its hit/miss tally. Sources with no adjudicable
    observation fall back to their prior mean (RFC 0088 §4 T3b).
    """
    observations = load_observations(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    if not observations:
        return {}
    tallies: dict[str, list[int]] = {obs.source_kind: [0, 0] for obs in observations}
    moves_by_bin: dict[str, list[TripEvent]] = {}
    for obs in observations:
        if obs.bin_code not in moves_by_bin:
            moves_by_bin[obs.bin_code] = load_bin_events(
                conn, obs.bin_code, tenant_id=tenant_id, corpus_id=corpus_id
            )
        verdict = adjudicate_observation(obs, moves_by_bin[obs.bin_code])
        if verdict is None:
            continue
        tally = tallies[obs.source_kind]
        tally[0 if verdict else 1] += 1
    return {
        source: source_reliability(source, hits=hits, misses=misses)
        for source, (hits, misses) in tallies.items()
    }
