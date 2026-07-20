"""Stash-run core: pure batch routing (BINK-24) + recorded runs (BINK-25).

A stash run routes a batch of typed or vision-derived item texts in one pass:
each item goes through :func:`binkeeper.bin_route.route_text_item` unchanged,
the results partition into a swipeable *deck* (the router recommends a bin) and
a *pending pile* (the router abstained), and the deck compiles into a wave plan
grouped so each destination bin is opened exactly once.

Recording a run (:func:`record_stash_run`) appends one append-only header row
and fans out into ordinary immutable routing receipts sharing the run id — the
receipt schema, idempotency handles, and decision ledger are untouched, and the
whole operation is idempotent at both the run and the receipt level.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.types.json import Jsonb

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_route import TextRoute, route_text_item

STASH_RUN_SCHEMA_VERSION = "stash_run.v1"


class StashRunError(ValueError):
    """Domain root for stash-run failures."""


@dataclass(frozen=True)
class StashItemRoute:
    """One batch item with its advisory route."""

    index: int
    text: str
    route: TextRoute

    @property
    def routable(self) -> bool:
        """Return whether the router recommends a bin (deck) or abstains (pending)."""
        return self.route.recommended_bin_code is not None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one batch item."""
        return {
            "index": self.index,
            "text": self.text,
            "disposition": "deck" if self.routable else "pending",
            "route": self.route.to_json(),
        }


@dataclass(frozen=True)
class WaveStop:
    """One destination bin in a wave plan, opened exactly once."""

    bin_code: str
    capacity_state: str
    item_indexes: tuple[int, ...]
    item_texts: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a wave stop."""
        return {
            "bin_code": self.bin_code,
            "capacity_state": self.capacity_state,
            "item_indexes": list(self.item_indexes),
            "item_texts": list(self.item_texts),
        }


@dataclass(frozen=True)
class StashBatchRoute:
    """A routed batch: deck, pending pile, wave plan, and abstention stats."""

    site: str
    items: tuple[StashItemRoute, ...]
    wave_plan: tuple[WaveStop, ...]

    @property
    def deck(self) -> tuple[StashItemRoute, ...]:
        """Return the routable items in batch order."""
        return tuple(item for item in self.items if item.routable)

    @property
    def pending(self) -> tuple[StashItemRoute, ...]:
        """Return the abstained items in batch order."""
        return tuple(item for item in self.items if not item.routable)

    @property
    def abstain_rate(self) -> float:
        """Return the abstained fraction of the batch (0.0 for an empty batch)."""
        return len(self.pending) / len(self.items) if self.items else 0.0

    def abstain_flag_counts(self) -> dict[str, int]:
        """Return how often each abstain flag occurred across the pending pile."""
        counts: dict[str, int] = {}
        for item in self.pending:
            for flag in item.route.abstain_flags:
                counts[flag] = counts.get(flag, 0) + 1
        return dict(sorted(counts.items()))

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the routed batch."""
        return {
            "site": self.site,
            "item_count": len(self.items),
            "deck_count": len(self.deck),
            "pending_count": len(self.pending),
            "abstain_rate": round(self.abstain_rate, 4),
            "abstain_flag_counts": self.abstain_flag_counts(),
            "wave_plan": [stop.to_json() for stop in self.wave_plan],
            "items": [item.to_json() for item in self.items],
        }


def route_batch(
    *,
    items: Sequence[str],
    site: str,
    passports: Sequence[BinPassport],
    liveness: Mapping[str, float] | None = None,
) -> StashBatchRoute:
    """Route a batch of item texts and compile the wave plan. Pure.

    Each item routes independently through the unchanged single-item router, so
    a batch of one is byte-identical to ``route_text_item``. The wave plan
    groups deck items by recommended bin, ordered by bin code (within-site walk
    order is campaign-C scope), so each destination bin appears exactly once.
    """
    routed = tuple(
        StashItemRoute(
            index=index,
            text=text,
            route=route_text_item(text=text, site=site, passports=passports, liveness=liveness),
        )
        for index, text in enumerate(items)
    )
    capacity_by_bin = {passport.bin_code: passport.capacity_state for passport in passports}
    by_bin: dict[str, list[StashItemRoute]] = {}
    for item in routed:
        code = item.route.recommended_bin_code
        if code is not None:
            by_bin.setdefault(code, []).append(item)
    wave_plan = tuple(
        WaveStop(
            bin_code=code,
            capacity_state=capacity_by_bin.get(code, "unknown"),
            item_indexes=tuple(item.index for item in members),
            item_texts=tuple(item.text for item in members),
        )
        for code, members in sorted(by_bin.items())
    )
    return StashBatchRoute(site=site, items=routed, wave_plan=wave_plan)


def stash_route_batch(
    conn: psycopg.Connection,
    *,
    items: Sequence[str],
    site: str,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> StashBatchRoute:
    """Load owner-corrected passports once and route the whole batch. Read-only."""
    from binkeeper.bin_placement_feedback import load_folded_bin_passports
    from binkeeper.bin_route import _load_liveness  # shared lane gate

    passports = load_folded_bin_passports(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    liveness = _load_liveness(conn, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    return route_batch(items=items, site=site, passports=passports, liveness=liveness)


@dataclass(frozen=True)
class StashRunRecord:
    """The result of recording one stash run and its per-item receipts."""

    stash_run_id: str
    external_id: str
    seq: int
    site: str
    already_existed: bool
    receipts: tuple[Mapping[str, object], ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a recorded run."""
        deck = sum(
            1
            for receipt in self.receipts
            if _route_result(receipt).get("recommended_bin_code") is not None
        )
        return {
            "stash_run_id": self.stash_run_id,
            "external_id": self.external_id,
            "seq": self.seq,
            "site": self.site,
            "already_existed": self.already_existed,
            "item_count": len(self.receipts),
            "deck_count": deck,
            "pending_count": len(self.receipts) - deck,
            "receipts": [dict(receipt) for receipt in self.receipts],
        }


def _route_result(receipt: Mapping[str, object]) -> Mapping[str, object]:
    result = receipt.get("route_result")
    return result if isinstance(result, Mapping) else {}


def stash_run_external_id(
    *, site: str, items: Sequence[str], requested_at: datetime, idempotency_key: str | None
) -> str:
    """Derive the idempotency handle for one stash run."""
    if idempotency_key and idempotency_key.strip():
        basis = f"key:{idempotency_key.strip()}"
    else:
        basis = json.dumps(
            {
                "site": site.strip(),
                "items": [item.strip() for item in items],
                "requested_at": requested_at.isoformat(),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    return f"stash-run:{hashlib.sha256(basis.encode('utf-8')).hexdigest()[:40]}"


def record_stash_run(
    conn: psycopg.Connection,
    *,
    items: Sequence[str],
    site: str,
    requested_at: datetime | None = None,
    source_kind: str = "text",
    source_ref: str | None = None,
    idempotency_key: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> StashRunRecord:
    """Append one stash-run header and fan out per-item routing receipts.

    Idempotent at the run level (same input or explicit key returns the
    original run and its receipts, appending nothing) and at the receipt
    level (each receipt's key derives from the run handle plus the item's
    batch position, so two identical item texts in one run still get their
    own receipts while a replay dedupes).
    """
    from binkeeper.bin_placement import TextRoutingRequest, record_text_routing_request

    cleaned = [item.strip() for item in items if item.strip()]
    if not cleaned:
        raise StashRunError("a stash run needs at least one item")
    if not site.strip():
        raise StashRunError("site is required")
    when = requested_at or datetime.now(UTC)
    external_id = stash_run_external_id(
        site=site, items=cleaned, requested_at=when, idempotency_key=idempotency_key
    )
    with conn.transaction():
        inserted = conn.execute(
            """
            INSERT INTO stash_runs (
                tenant_id, corpus_id, external_id, site, source_kind, source_ref,
                item_count, requested_at, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, corpus_id, external_id) DO NOTHING
            RETURNING id::text, seq
            """,
            (
                tenant_id,
                corpus_id,
                external_id,
                site.strip(),
                source_kind,
                source_ref,
                len(cleaned),
                when,
                Jsonb(
                    {
                        "schema_version": STASH_RUN_SCHEMA_VERSION,
                        "idempotency_key": idempotency_key,
                        "items": cleaned,
                    }
                ),
            ),
        ).fetchone()
    if inserted is None:
        return _existing_stash_run(conn, external_id, tenant_id=tenant_id, corpus_id=corpus_id)
    run_id = str(inserted[0])
    receipts = []
    for index, text in enumerate(cleaned):
        record = record_text_routing_request(
            conn,
            TextRoutingRequest(
                text=text,
                site=site,
                requested_at=when,
                source_label="stash-run",
                idempotency_key=f"{external_id}:{index}",
                stash_run_id=run_id,
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            ),
        )
        receipts.append(record.to_json())
    return StashRunRecord(
        stash_run_id=run_id,
        external_id=external_id,
        seq=int(inserted[1]),
        site=site.strip(),
        already_existed=False,
        receipts=tuple(receipts),
    )


_PLACED_DECISION_KINDS = frozenset({"accept", "override", "create_new_bin"})
WAVE_STOP_SCHEMA_VERSION = "wave_stop.v1"


@dataclass(frozen=True)
class WavePlanStop:
    """One destination bin on the walkable wave, opened exactly once."""

    bin_code: str
    capacity_state: str
    item_texts: tuple[str, ...]
    completed: bool

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one wave stop."""
        return {
            "bin_code": self.bin_code,
            "capacity_state": self.capacity_state,
            "item_texts": list(self.item_texts),
            "completed": self.completed,
        }


@dataclass(frozen=True)
class WavePlanView:
    """The walkable put-away plan compiled from a run's owner decisions."""

    stash_run_id: str
    site: str
    stops: tuple[WavePlanStop, ...]

    @property
    def completed_count(self) -> int:
        """Return how many stops already carry completion evidence."""
        return sum(1 for stop in self.stops if stop.completed)

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for the wave plan."""
        return {
            "stash_run_id": self.stash_run_id,
            "site": self.site,
            "stop_count": len(self.stops),
            "completed_count": self.completed_count,
            "stops": [stop.to_json() for stop in self.stops],
        }


def wave_plan_for_run(
    conn: psycopg.Connection,
    *,
    stash_run_id: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> WavePlanView:
    """Compile the wave from the run's placed decisions. Read-only.

    Groups every placed decision (accept, override, create_new_bin) by its
    selected bin so each destination is opened exactly once, orders stops by
    bin code (within-site walk order is campaign-C scope), annotates each with
    the passport's capacity state, and marks stops whose completion capture
    already exists.
    """
    run = conn.execute(
        """
        SELECT id::text, site FROM stash_runs
        WHERE tenant_id = %s AND corpus_id = %s AND id::text = %s
        """,
        (tenant_id, corpus_id, stash_run_id),
    ).fetchone()
    if run is None:
        raise LookupError(f"unknown stash run {stash_run_id!r}")
    placed = _placed_items(conn, stash_run_id, tenant_id=tenant_id, corpus_id=corpus_id)
    capacity = _capacity_by_bin(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    completed = _completed_stops(conn, stash_run_id, tenant_id=tenant_id, corpus_id=corpus_id)
    stops = tuple(
        WavePlanStop(
            bin_code=bin_code,
            capacity_state=capacity.get(bin_code, "unknown"),
            item_texts=tuple(texts),
            completed=bin_code in completed,
        )
        for bin_code, texts in sorted(placed.items())
    )
    return WavePlanView(stash_run_id=str(run[0]), site=str(run[1]), stops=stops)


def complete_wave_stop(
    conn: psycopg.Connection,
    *,
    stash_run_id: str,
    bin_code: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> bool:
    """Append the completion evidence for one wave stop. Idempotent.

    The capture is an additive content note on the destination bin — the
    placed item texts join the bin's recorded contents, enriching its
    passport vocabulary — plus a wave marker used to mark the stop done.
    Returns True when the capture is new, False on a replay.
    """
    from binkeeper.personal_memory import CaptureRequest, PersonalMemoryService

    placed = _placed_items(conn, stash_run_id, tenant_id=tenant_id, corpus_id=corpus_id)
    items = placed.get(bin_code)
    if not items:
        raise StashRunError(f"stash run has no placed items for bin {bin_code!r}")
    idempotency_key = f"wavestop:{stash_run_id}:{bin_code}"
    observed_at = _stable_wave_time(conn, idempotency_key, tenant_id=tenant_id, corpus_id=corpus_id)
    result = PersonalMemoryService(conn).capture(
        CaptureRequest(
            text="; ".join(items),
            capture_type="observation",
            privacy_tier=2,
            observed_at=observed_at,
            source_label="stash-wave",
            idempotency_key=idempotency_key,
            metadata={
                "kind": "bin_capture",
                "schema_version": WAVE_STOP_SCHEMA_VERSION,
                "bin_code": bin_code,
                "captured_at": observed_at.isoformat(),
                "wave": {"stash_run_id": stash_run_id},
            },
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    )
    return not result.already_existed


def _stable_wave_time(
    conn: psycopg.Connection,
    idempotency_key: str,
    *,
    tenant_id: str,
    corpus_id: str,
) -> datetime:
    """Use now() on first write and the stored time on a replay (bin_manage pattern)."""
    row = conn.execute(
        """
        SELECT observed_at FROM captures
        WHERE tenant_id = %s AND corpus_id = %s
          AND raw_payload->>'source_label' = 'stash-wave'
          AND raw_payload->>'idempotency_key' = %s
        LIMIT 1
        """,
        (tenant_id, corpus_id, idempotency_key),
    ).fetchone()
    if row is not None and isinstance(row[0], datetime):
        return row[0]
    return datetime.now(UTC)


def _placed_items(
    conn: psycopg.Connection,
    stash_run_id: str,
    *,
    tenant_id: str,
    corpus_id: str,
) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT r.input_text, d.decision_kind, d.selected_bin_code
        FROM bin_routing_requests r
        JOIN LATERAL (
            SELECT decision_kind, selected_bin_code
            FROM bin_placement_decisions d
            WHERE d.tenant_id = r.tenant_id
              AND d.corpus_id = r.corpus_id
              AND d.routing_request_id = r.id
            ORDER BY d.seq DESC
            LIMIT 1
        ) d ON TRUE
        WHERE r.tenant_id = %s AND r.corpus_id = %s AND r.stash_run_id = %s
        ORDER BY r.seq
        """,
        (tenant_id, corpus_id, stash_run_id),
    ).fetchall()
    placed: dict[str, list[str]] = {}
    for text, kind, selected in rows:
        if str(kind) in _PLACED_DECISION_KINDS and selected:
            placed.setdefault(str(selected), []).append(str(text))
    return placed


def _capacity_by_bin(conn: psycopg.Connection, *, tenant_id: str, corpus_id: str) -> dict[str, str]:
    from binkeeper.bin_passport import load_bin_passports

    return {
        passport.bin_code: passport.capacity_state
        for passport in load_bin_passports(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    }


def _completed_stops(
    conn: psycopg.Connection,
    stash_run_id: str,
    *,
    tenant_id: str,
    corpus_id: str,
) -> frozenset[str]:
    rows = conn.execute(
        """
        SELECT raw_payload->'metadata'->>'bin_code'
        FROM captures
        WHERE tenant_id = %s AND corpus_id = %s
          AND raw_payload->'metadata'->'wave'->>'stash_run_id' = %s
        """,
        (tenant_id, corpus_id, stash_run_id),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows if row[0])


def _existing_stash_run(
    conn: psycopg.Connection,
    external_id: str,
    *,
    tenant_id: str,
    corpus_id: str,
) -> StashRunRecord:
    row = conn.execute(
        """
        SELECT id::text, seq, site FROM stash_runs
        WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
        """,
        (tenant_id, corpus_id, external_id),
    ).fetchone()
    if row is None:  # pragma: no cover - conflict without a row is impossible
        raise StashRunError("idempotent stash-run insert conflicted without a prior row")
    receipt_rows = conn.execute(
        """
        SELECT external_id, id::text, seq, route_result_sha256, route_result_json
        FROM bin_routing_requests
        WHERE tenant_id = %s AND corpus_id = %s AND stash_run_id = %s
        ORDER BY seq
        """,
        (tenant_id, corpus_id, row[0]),
    ).fetchall()
    receipts = tuple(
        {
            "routing_request_id": str(receipt[1]),
            "external_id": str(receipt[0]),
            "seq": int(receipt[2]),
            "already_existed": True,
            "route_result_sha256": str(receipt[3]),
            "route_result": receipt[4],
        }
        for receipt in receipt_rows
    )
    return StashRunRecord(
        stash_run_id=str(row[0]),
        external_id=external_id,
        seq=int(row[1]),
        site=str(row[2]),
        already_existed=True,
        receipts=receipts,
    )


# --- quorum-born bins (BINK-28) -----------------------------------------------

# A cluster founds a bin only when this many mutually-similar orphans gather.
BIN_QUORUM_SIZE = int(os.environ.get("BINKEEPER_BIN_QUORUM_SIZE", "4"))
# Minimum pairwise token overlap (|a∩b| / min(|a|,|b|)) between EVERY pair in a
# cluster. Mutual similarity, never router scores: the BINK-24 spike showed
# vague labels share a near-constant structural score and would false-cluster.
BIN_QUORUM_OVERLAP = float(os.environ.get("BINKEEPER_BIN_QUORUM_OVERLAP", "0.5"))
# Only orphans the router could not match to ANY passport may found a bin;
# a close-tie abstention belongs to the tie, not to a new bin.
_FOUNDER_FLAGS = frozenset({"no_accepting_passport", "no_passports"})


@dataclass(frozen=True)
class OrphanCluster:
    """One quorum of mutually-similar unroutable items, ready to found a bin."""

    receipt_ids: tuple[str, ...]
    texts: tuple[str, ...]
    shared_tokens: tuple[str, ...]
    drafted_theme: str
    drafted_accepts: tuple[str, ...]

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for one proposal."""
        return {
            "receipt_ids": list(self.receipt_ids),
            "texts": list(self.texts),
            "shared_tokens": list(self.shared_tokens),
            "drafted_theme": self.drafted_theme,
            "drafted_accepts": list(self.drafted_accepts),
        }


def cluster_orphans(
    orphans: Sequence[tuple[str, str]],
    *,
    min_overlap: float = BIN_QUORUM_OVERLAP,
    quorum: int = BIN_QUORUM_SIZE,
) -> list[OrphanCluster]:
    """Group orphans into mutually-similar clusters of at least quorum size. Pure.

    Complete-linkage on token overlap: an orphan joins a cluster only when it
    clears ``min_overlap`` against EVERY existing member, so two unrelated
    items can never chain into one cluster through a vague middle item. The
    drafted passport derives from exactly the founding items: accepts are the
    tokens shared by at least two members, the theme is the strongest shared
    phrase.
    """
    from binkeeper.bin_route import _tokens_for

    tokened = [
        (receipt_id, text, _tokens_for(text)) for receipt_id, text in orphans if text.strip()
    ]
    clusters: list[list[tuple[str, str, set[str]]]] = []
    for entry in tokened:
        placed = False
        for cluster in clusters:
            if all(_mutual_overlap(entry[2], member[2]) >= min_overlap for member in cluster):
                cluster.append(entry)
                placed = True
                break
        if not placed:
            clusters.append([entry])
    proposals: list[OrphanCluster] = []
    for cluster in clusters:
        if len(cluster) < quorum:
            continue
        token_counts: dict[str, int] = {}
        for _, _, tokens in cluster:
            for token in tokens:
                token_counts[token] = token_counts.get(token, 0) + 1
        shared = tuple(sorted(token for token, count in token_counts.items() if count >= 2))
        if not shared:
            continue
        proposals.append(
            OrphanCluster(
                receipt_ids=tuple(entry[0] for entry in cluster),
                texts=tuple(entry[1] for entry in cluster),
                shared_tokens=shared,
                drafted_theme=" ".join(shared[:4]),
                drafted_accepts=shared,
            )
        )
    return proposals


def _mutual_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def load_orphans(
    conn: psycopg.Connection,
    *,
    site: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[tuple[str, str]]:
    """Load founder-eligible orphans at a site: abstained with no passport match,
    and either undecided or explicitly rejected. Read-only."""
    rows = conn.execute(
        """
        SELECT r.id::text, r.input_text, r.route_result_json
        FROM bin_routing_requests r
        LEFT JOIN LATERAL (
            SELECT decision_kind FROM bin_placement_decisions d
            WHERE d.tenant_id = r.tenant_id AND d.corpus_id = r.corpus_id
              AND d.routing_request_id = r.id
            ORDER BY d.seq DESC LIMIT 1
        ) d ON TRUE
        WHERE r.tenant_id = %s AND r.corpus_id = %s AND r.site = %s
          AND r.route_result_json->>'recommended_bin_code' IS NULL
          AND (d.decision_kind IS NULL OR d.decision_kind = 'reject')
        ORDER BY r.seq
        """,
        (tenant_id, corpus_id, site),
    ).fetchall()
    orphans: list[tuple[str, str]] = []
    for receipt_id, text, route_result in rows:
        flags = route_result.get("abstain_flags") if isinstance(route_result, Mapping) else None
        flag_set = {str(flag) for flag in flags} if isinstance(flags, list) else set()
        if flag_set & _FOUNDER_FLAGS:
            orphans.append((str(receipt_id), str(text)))
    return orphans


def quorum_proposals(
    conn: psycopg.Connection,
    *,
    site: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[OrphanCluster]:
    """Cluster a site's founder-eligible orphans into bin proposals. Read-only."""
    return cluster_orphans(load_orphans(conn, site=site, tenant_id=tenant_id, corpus_id=corpus_id))


def found_bin_from_cluster(
    conn: psycopg.Connection,
    *,
    receipt_ids: Sequence[str],
    bin_code: str,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> int:
    """Write one create_new_bin decision per founding orphan. Owner-approved only.

    The decisions are the bin's birth certificate: every founding item links to
    the new code. Registration and label printing then proceed through the
    normal reviewed flow. Idempotent per (bin, receipt).
    """
    from binkeeper.bin_placement import PlacementDecisionAppend, record_placement_decision

    code = bin_code.strip().upper()
    if not code:
        raise StashRunError("a founding bin code is required")
    written = 0
    for receipt_id in receipt_ids:
        result = record_placement_decision(
            conn,
            PlacementDecisionAppend(
                routing_request_id=receipt_id,
                decision_kind="create_new_bin",
                selected_bin_code=code,
                idempotency_key=f"quorum:{code}:{receipt_id}",
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            ),
        )
        if not result.already_existed:
            written += 1
    return written
