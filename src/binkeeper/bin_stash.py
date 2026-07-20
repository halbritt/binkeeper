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
