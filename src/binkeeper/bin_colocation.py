"""Witnessed anchor↔bin co-location evidence (BINK-31).

Any photo whose decoded symbols include at least one LOC- anchor and one bin
label yields one immutable observation per (anchor, member) pair. Strength is
geometry-weighted from the QR bounding rects pyzbar already returns: anchors
print physically larger, so an anchor that appears at least as large as the
member label reads as "this shelf", while a small, distant anchor — or a
second anchor farther away in a multi-anchor frame — records low strength
instead of confident error. Advisory by construction: rows corroborate the
containment fold (BINK-32) and never move a bin.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from binkeeper.bin_geo import DecodedCode
from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID

COLOCATION_SCHEMA_VERSION = "colocation_observation.v1"
# Geometry thresholds: the anchor-to-member apparent-size ratio above which the
# pair reads as same-shelf, and the floor under which it reads as a wide shot.
_CONFIDENT_SIZE_RATIO = 0.8
_PLAUSIBLE_SIZE_RATIO = 0.4
# A member pairs at full strength only with its NEAREST anchor; other anchors
# in the same frame record this residual strength.
_SECONDARY_ANCHOR_STRENGTH = 0.2


class BinColocationError(ValueError):
    """Domain root for co-location failures."""


@dataclass(frozen=True)
class ColocationPair:
    """One geometry-weighted anchor↔member pairing from a single frame."""

    anchor_code: str
    member_code: str
    strength: float

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one pairing."""
        return {
            "anchor_code": self.anchor_code,
            "member_code": self.member_code,
            "strength": round(self.strength, 4),
        }


def colocation_pairs(decoded: Sequence[DecodedCode]) -> list[ColocationPair]:
    """Pair every member with every anchor in the frame, geometry-weighted. Pure.

    Full computed strength goes to the member's nearest anchor (rect-center
    distance); any other anchors in the frame record the residual strength, so
    a two-shelf wide shot cannot claim both shelves confidently.
    """
    anchors = [entry for entry in decoded if entry.is_anchor]
    members = [entry for entry in decoded if not entry.is_anchor]
    pairs: list[ColocationPair] = []
    for member in members:
        ranked = sorted(anchors, key=lambda anchor: _center_distance(anchor, member))
        for rank, anchor in enumerate(ranked):
            strength = _size_strength(anchor, member)
            if rank > 0:
                strength = min(strength, _SECONDARY_ANCHOR_STRENGTH)
            pairs.append(
                ColocationPair(anchor_code=anchor.code, member_code=member.code, strength=strength)
            )
    return pairs


def _size_strength(anchor: DecodedCode, member: DecodedCode) -> float:
    anchor_area = max(0, anchor.width) * max(0, anchor.height)
    member_area = max(0, member.width) * max(0, member.height)
    if anchor_area <= 0 or member_area <= 0:
        return _SECONDARY_ANCHOR_STRENGTH  # no geometry: record only weak evidence
    ratio = anchor_area / member_area
    if ratio >= _CONFIDENT_SIZE_RATIO:
        return 1.0
    if ratio >= _PLAUSIBLE_SIZE_RATIO:
        return 0.6
    return 0.3


def _center_distance(a: DecodedCode, b: DecodedCode) -> float:
    ax, ay = a.left + a.width / 2, a.top + a.height / 2
    bx, by = b.left + b.width / 2, b.top + b.height / 2
    return float((ax - bx) ** 2 + (ay - by) ** 2)


def record_colocation(
    conn: psycopg.Connection,
    *,
    anchor_code: str,
    member_code: str,
    strength: float,
    observed_at: datetime | None = None,
    source_kind: str = "qr_colocation",
    evidence_ref: str | None = None,
    idempotency_key: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> bool:
    """Append one immutable co-location observation. Idempotent.

    Returns True when the row is new, False on a replay. Never relocates a
    bin: this table is corroborating evidence for the containment fold only.
    """
    if not anchor_code.startswith("LOC-"):
        raise BinColocationError(f"anchor_code must be a LOC- code, got {anchor_code!r}")
    if not member_code.strip():
        raise BinColocationError("member_code is required")
    when = observed_at or datetime.now(UTC)
    clamped = max(0.0, min(1.0, strength))
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = json.dumps(
            {
                "anchor_code": anchor_code,
                "member_code": member_code,
                "observed_at": when.isoformat(),
                "evidence_ref": evidence_ref,
                "tenant_id": tenant_id,
                "corpus_id": corpus_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    external_id = f"coloc:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:40]}"
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO colocation_observations (
                tenant_id, corpus_id, external_id, anchor_code, member_code,
                observed_at, source_kind, observation_strength, evidence_ref, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                anchor_code,
                member_code.strip(),
                when,
                source_kind,
                clamped,
                evidence_ref,
                Jsonb({"schema_version": COLOCATION_SCHEMA_VERSION}),
            ),
        ).fetchone()
    return inserted is not None


def harvest_photo_colocations(
    conn: psycopg.Connection,
    image: bytes,
    *,
    observed_at: datetime | None = None,
    evidence_ref: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[ColocationPair]:
    """Decode one photo and append its anchor↔member pairs. Advisory.

    Returns the appended pairs (empty when the frame holds no anchor+member
    combination or decoding is unavailable). Errors in decoding degrade to an
    empty harvest; the surrounding photo flow must never fail because this
    advisory lane did.
    """
    from binkeeper.bin_geo import decode_all_codes

    pairs = colocation_pairs(decode_all_codes(image))
    when = observed_at or datetime.now(UTC)
    for pair in pairs:
        record_colocation(
            conn,
            anchor_code=pair.anchor_code,
            member_code=pair.member_code,
            strength=pair.strength,
            observed_at=when,
            evidence_ref=evidence_ref,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    return pairs


# --- containment fold (BINK-32) -----------------------------------------------

# Containment decays on its own clock: shelves reshuffle faster than sites.
ANCHOR_HALF_LIFE_DAYS = float(os.environ.get("BINKEEPER_ANCHOR_HALF_LIFE_DAYS", "90"))
# Below this confidence the shelf tier abstains and answers fall back to site.
ANCHOR_CONTAINMENT_FLOOR = float(os.environ.get("BINKEEPER_ANCHOR_FLOOR", "0.5"))
# Confidence drop per sighting under a DIFFERENT anchor after the winner's last.
ANCHOR_CONTRADICTION_SHOCK = float(os.environ.get("BINKEEPER_ANCHOR_CONTRADICTION_SHOCK", "0.4"))
# An anchor unseen in ANY photo for this long demotes to unverified: it drops
# out of where-answers and routing citations even where accumulated old mass
# would still clear the confidence floor, and becomes a sweep target instead.
ANCHOR_DEMOTION_DAYS = float(os.environ.get("BINKEEPER_ANCHOR_DEMOTION_DAYS", "180"))


@dataclass(frozen=True)
class ColocationSighting:
    """One loaded co-location row, as the fold sees it."""

    anchor_code: str
    observed_at: datetime
    strength: float


@dataclass(frozen=True)
class ContainmentBelief:
    """A bin's witnessed shelf, with decayed confidence and an abstain verdict."""

    bin_code: str
    anchor_code: str | None
    confidence: float
    age_days: float | None
    observation_count: int
    abstained: bool

    def to_json(self) -> dict[str, object] | None:
        """Return the shelf tier for a where-answer, or None when abstained."""
        if self.abstained or self.anchor_code is None:
            return None
        return {
            "anchor_code": self.anchor_code,
            "confidence": round(self.confidence, 4),
            "age_days": round(self.age_days, 2) if self.age_days is not None else None,
            "observation_count": self.observation_count,
        }


def fold_containment(
    bin_code: str,
    sightings: Sequence[ColocationSighting],
    *,
    now: datetime,
    half_life_days: float = ANCHOR_HALF_LIFE_DAYS,
    floor: float = ANCHOR_CONTAINMENT_FLOOR,
    shock: float = ANCHOR_CONTRADICTION_SHOCK,
    demoted: frozenset[str] = frozenset(),
) -> ContainmentBelief:
    """Fold witnessed sightings into the bin's current shelf. Pure.

    The anchor with the highest decayed evidence mass wins; sightings under a
    different anchor after the winner's latest sighting shock its confidence,
    exactly as `contradict` shocks the site fold. Below the floor the tier
    abstains — shelf answers must fall back to site level rather than guess,
    and they never outrank the site fold.
    """
    if not sightings:
        return ContainmentBelief(bin_code, None, 0.0, None, 0, True)
    mass: dict[str, float] = {}
    latest: dict[str, datetime] = {}
    for sighting in sightings:
        age_days = max(0.0, (now - sighting.observed_at).total_seconds() / 86400.0)
        decayed = sighting.strength * (
            0.5 ** (age_days / half_life_days) if half_life_days > 0 else 0.0
        )
        mass[sighting.anchor_code] = mass.get(sighting.anchor_code, 0.0) + decayed
        seen = latest.get(sighting.anchor_code)
        if seen is None or sighting.observed_at > seen:
            latest[sighting.anchor_code] = sighting.observed_at
    winner = max(mass, key=lambda code: (mass[code], latest[code]))
    contradictions = sum(
        1
        for sighting in sightings
        if sighting.anchor_code != winner and sighting.observed_at > latest[winner]
    )
    confidence = max(0.0, min(1.0, mass[winner]) - shock * contradictions)
    age_days = (now - latest[winner]).total_seconds() / 86400.0
    return ContainmentBelief(
        bin_code=bin_code,
        anchor_code=winner,
        confidence=confidence,
        age_days=age_days,
        observation_count=len(sightings),
        abstained=confidence < floor or winner in demoted,
    )


def load_bin_sightings(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[ColocationSighting]:
    """Load one bin's co-location rows in observation order. Read-only."""
    rows = conn.execute(
        """
        SELECT anchor_code, observed_at, observation_strength
        FROM colocation_observations
        WHERE tenant_id = %s AND corpus_id = %s AND member_code = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id, bin_code),
    ).fetchall()
    return [
        ColocationSighting(anchor_code=str(row[0]), observed_at=row[1], strength=float(row[2]))
        for row in rows
    ]


def bin_containment(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> ContainmentBelief:
    """Fold one bin's witnessed shelf from the ledger. Read-only."""
    when = now or datetime.now(UTC)
    sightings = load_bin_sightings(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
    demoted = demoted_anchors(conn, now=when, tenant_id=tenant_id, corpus_id=corpus_id)
    return fold_containment(bin_code, sightings, now=when, demoted=demoted)


def containment_by_bin(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> dict[str, ContainmentBelief]:
    """Fold every witnessed bin's shelf in one pass. Read-only."""
    rows = conn.execute(
        """
        SELECT member_code, anchor_code, observed_at, observation_strength
        FROM colocation_observations
        WHERE tenant_id = %s AND corpus_id = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    grouped: dict[str, list[ColocationSighting]] = {}
    for member, anchor, observed_at, strength in rows:
        grouped.setdefault(str(member), []).append(
            ColocationSighting(
                anchor_code=str(anchor), observed_at=observed_at, strength=float(strength)
            )
        )
    when = now or datetime.now(UTC)
    demoted = demoted_anchors(conn, now=when, tenant_id=tenant_id, corpus_id=corpus_id)
    return {
        member: fold_containment(member, sightings, now=when, demoted=demoted)
        for member, sightings in grouped.items()
    }


def demoted_anchors(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    horizon_days: float = ANCHOR_DEMOTION_DAYS,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> frozenset[str]:
    """Anchors whose latest sighting anywhere is older than the horizon. Read-only.

    Demotion is a fold outcome, not a mutation: no row changes, the anchor
    simply stops being served and surfaces as a sweep target instead.
    """
    when = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT anchor_code, max(observed_at)
        FROM colocation_observations
        WHERE tenant_id = %s AND corpus_id = %s
        GROUP BY anchor_code
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    return frozenset(
        str(row[0])
        for row in rows
        if row[1] is not None and (when - row[1]).total_seconds() / 86400.0 > horizon_days
    )
