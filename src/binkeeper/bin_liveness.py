"""RFC 0088 T3b transcript-deixis LIVENESS lane (never location).

Mines mentions of the known bin vocabulary out of the owner's approved local text
(chats, captures, notes) to corroborate that an item exists and is in current use.
This is a recency/liveness signal ONLY: it never says where a bin is, never writes
a ``location_observation``, and never relocates a bin. Post D160 the approved
source set is owner-life conversational text and captures -- explicitly NOT the
removed work corpus (``claude_code``) and NOT email (``gmail``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import load_bin_passports

BIN_LIVENESS_HARVESTER_VERSION: Final[str] = os.environ.get(
    "BINKEEPER_BIN_LIVENESS_HARVESTER_VERSION", "bin-liveness.v1.rfc0088-t3b"
)
# Approved owner-life text only. gmail (email), git (code), and claude_code (the
# removed work corpus) are deliberately excluded; owner decision 2026-07-07.
_DEFAULT_APPROVED_SOURCE_KINDS: Final[tuple[str, ...]] = (
    "claude",
    "chatgpt",
    "gemini",
    "capture",
    "obsidian",
)
APPROVED_LIVENESS_SOURCE_KINDS: Final[tuple[str, ...]] = tuple(
    kind.strip()
    for kind in os.environ.get(
        "BINKEEPER_BIN_LIVENESS_SOURCE_KINDS", ",".join(_DEFAULT_APPROVED_SOURCE_KINDS)
    ).split(",")
    if kind.strip()
)
_EXCLUDED_SOURCE_KINDS: Final[frozenset[str]] = frozenset({"gmail", "git", "claude_code"})
BIN_LIVENESS_TIER_MAX: Final[int] = int(os.environ.get("BINKEEPER_BIN_LIVENESS_TIER_MAX", "1"))
BIN_LIVENESS_HALF_LIFE_DAYS: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_LIVENESS_HALF_LIFE_DAYS", "45")
)
_MIN_PHRASE_LEN: Final[int] = 4
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class BinLivenessError(ValueError):
    """Domain root for liveness-lane failures (RFC 0012 family)."""


@dataclass(frozen=True)
class HarvestResult:
    """Summary of one harvest pass."""

    phrases_scanned: int
    mentions_recorded: int
    approved_source_kinds: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a harvest result."""
        return {
            "phrases_scanned": self.phrases_scanned,
            "mentions_recorded": self.mentions_recorded,
            "approved_source_kinds": list(self.approved_source_kinds),
        }


def normalize_phrase(phrase: str) -> str:
    """Normalize an item phrase for the liveness key (casefold, collapse tokens)."""
    return " ".join(match.group(0).lower() for match in _TOKEN_RE.finditer(phrase))


def known_item_phrases(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[str]:
    """Collect the item phrases BinKeeper knows about from bin passports."""
    seen: dict[str, str] = {}
    for passport in load_bin_passports(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id):
        candidates: list[str] = [
            *passport.accepts,
            *passport.examples,
            *passport.sibling_contents,
        ]
        if passport.theme:
            candidates.append(passport.theme)
        for phrase in candidates:
            norm = normalize_phrase(phrase)
            if len(norm) >= _MIN_PHRASE_LEN and norm not in seen:
                seen[norm] = phrase.strip()
    return list(seen.values())


def harvest_item_liveness(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    source_kinds: Sequence[str] = APPROVED_LIVENESS_SOURCE_KINDS,
    tier_max: int = BIN_LIVENESS_TIER_MAX,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> HarvestResult:
    """Mine approved owner-life text for mentions of the known bin vocabulary.

    Append-only and idempotent per (phrase, source evidence). Refuses any excluded
    source kind (email/work-corpus/code) outright rather than silently dropping it.
    """
    approved = _approved_source_kinds(source_kinds)
    phrases = known_item_phrases(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    recorded = 0
    for phrase in phrases:
        norm = normalize_phrase(phrase)
        pattern = rf"\m{re.escape(norm)}\M"
        rows = conn.execute(
            """
            SELECT source_kind::text AS source_kind, external_id AS source_ref,
                   COALESCE(observed_at, imported_at) AS mentioned_at
            FROM captures
            WHERE source_kind = ANY(%s) AND privacy_tier <= %s
              AND COALESCE(observed_at, imported_at) IS NOT NULL
              AND content_text IS NOT NULL AND content_text ~* %s
            """,
            (approved, tier_max, pattern),
        ).fetchall()
        for source_kind, source_ref, mentioned_at in rows:
            inserted = conn.execute(
                """
                INSERT INTO bin_item_liveness (
                    tenant_id, corpus_id, phrase_norm, item_phrase, source_kind,
                    source_ref, mentioned_at, harvester_version
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, corpus_id, phrase_norm, source_kind, source_ref)
                    DO NOTHING
                RETURNING id
                """,
                (
                    tenant_id,
                    corpus_id,
                    norm,
                    phrase,
                    source_kind,
                    source_ref,
                    mentioned_at,
                    BIN_LIVENESS_HARVESTER_VERSION,
                ),
            ).fetchone()
            if inserted is not None:
                recorded += 1
    return HarvestResult(len(phrases), recorded, tuple(approved))


def item_liveness_scores(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    half_life_days: float = BIN_LIVENESS_HALF_LIFE_DAYS,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> dict[str, float]:
    """Read-time decayed recency in (0, 1] per phrase from its latest mention."""
    when = now or datetime.now(UTC)
    rows = conn.execute(
        """
        SELECT phrase_norm, max(mentioned_at) AS latest
        FROM bin_item_liveness
        WHERE tenant_id = %s AND corpus_id = %s
        GROUP BY phrase_norm
        """,
        (tenant_id, corpus_id),
    ).fetchall()
    scores: dict[str, float] = {}
    for phrase_norm, latest in rows:
        scores[str(phrase_norm)] = _decayed_recency(when, latest, half_life_days)
    return scores


def _decayed_recency(now: datetime, latest: datetime | None, half_life_days: float) -> float:
    if latest is None or half_life_days <= 0:
        return 0.0
    latest_aware = latest if latest.tzinfo is not None else latest.replace(tzinfo=UTC)
    age = now - latest_aware
    if age < timedelta(0):
        return 1.0
    return float(0.5 ** (age.total_seconds() / (half_life_days * 86400.0)))


def _approved_source_kinds(source_kinds: Sequence[str]) -> list[str]:
    approved = [kind.strip() for kind in source_kinds if kind and kind.strip()]
    if not approved:
        raise BinLivenessError("no approved liveness source kinds configured")
    forbidden = _EXCLUDED_SOURCE_KINDS.intersection(approved)
    if forbidden:
        raise BinLivenessError(
            f"liveness source policy forbids {sorted(forbidden)}: "
            "email, code, and the removed work corpus are not owner-life text"
        )
    return approved
