"""Sub-location anchor labels (BINK-30).

An anchor is a LOC- code in the ordinary label grammar, printed large and
mounted on a shelf, cabinet, or room edge. It has no database row at print
time: the anchor is *born* later, the first time a photo shows it co-located
with a bin (BINK-31). Printing follows the established reviewed print-intent
discipline — a durable one-attempt reservation is appended before any printer
I/O, and an exact replay never reaches the printer again.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import psycopg

from binkeeper.bin_geo import is_anchor_code, normalize_bin_code
from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID

ANCHOR_PRINT_SCHEMA_VERSION = "anchor_print_intent.v1"
ANCHOR_LABEL_THEME = "Location anchor"
AnchorPrintOutcome = Literal["sent", "replayed", "failed", "unknown"]


class BinAnchorError(ValueError):
    """Domain root for anchor failures."""


@dataclass(frozen=True)
class AnchorPrintResult:
    """The outcome of one reviewed anchor print request."""

    anchor_code: str
    outcome: AnchorPrintOutcome

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON result shape."""
        return {"anchor_code": self.anchor_code, "outcome": self.outcome}


def print_anchor_label(
    conn: psycopg.Connection,
    *,
    anchor_code: str,
    action_id: str,
    label_text: str | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> AnchorPrintResult:
    """Reserve and attempt exactly one anchor label print.

    Mirrors the bin reprint flow: the immutable reservation capture lands
    before printer I/O, a replayed action never prints twice, and a timeout is
    reported as unknown rather than retried.
    """
    from binkeeper import bin_label
    from binkeeper.personal_memory import CaptureRequest, PersonalMemoryService

    code = normalize_bin_code(anchor_code)
    if code is None or not is_anchor_code(code):
        raise BinAnchorError(f"not an anchor code: {anchor_code!r} (expected LOC-...)")
    if not action_id.strip():
        raise BinAnchorError("action_id is required")
    queue = bin_label.BIN_LABEL_CUPS_QUEUE.strip()
    if not queue:
        return AnchorPrintResult(code, "failed")
    tspl = bin_label.render_tspl(code, theme=label_text or ANCHOR_LABEL_THEME)
    idempotency_key = f"anchorprint:{code}:{action_id.strip()}"
    reservation = PersonalMemoryService(conn).capture(
        CaptureRequest(
            text=f"anchor label print intent for {code}",
            capture_type="action",
            privacy_tier=2,
            observed_at=_stable_time(conn, idempotency_key, tenant_id, corpus_id),
            source_label="anchor-print",
            idempotency_key=idempotency_key,
            metadata={
                "kind": "anchor_print_intent",
                "schema_version": ANCHOR_PRINT_SCHEMA_VERSION,
                "anchor_code": code,
                "queue": queue,
                "tspl_sha256": hashlib.sha256(tspl.encode("utf-8")).hexdigest(),
            },
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    )
    if reservation.already_existed:
        return AnchorPrintResult(code, "replayed")
    try:
        bin_label.send_to_printer(tspl, cups_queue=queue)
    except bin_label.BinLabelError as exc:
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            return AnchorPrintResult(code, "unknown")
        return AnchorPrintResult(code, "failed")
    return AnchorPrintResult(code, "sent")


def _stable_time(
    conn: psycopg.Connection, idempotency_key: str, tenant_id: str, corpus_id: str
) -> datetime:
    """Use now() on first write and the stored time on a replay."""
    row = conn.execute(
        """
        SELECT observed_at FROM captures
        WHERE tenant_id = %s AND corpus_id = %s
          AND raw_payload->>'source_label' = 'anchor-print'
          AND raw_payload->>'idempotency_key' = %s
        LIMIT 1
        """,
        (tenant_id, corpus_id, idempotency_key),
    ).fetchone()
    if row is not None and isinstance(row[0], datetime):
        return row[0]
    return datetime.now(UTC)
