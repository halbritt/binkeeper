"""Durable one-attempt printer handoff, shared by every reviewed label action."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from subprocess import TimeoutExpired
from typing import Literal
from uuid import UUID

import psycopg

from binkeeper import bin_label
from binkeeper.bin_b1 import B1PrintUnknown
from binkeeper.bin_manage import (
    DEFAULT_BIN_ACTION_SCOPE,
    BinActionScope,
    BinLabelPrintIntent,
    BinManageError,
    reserve_label_print_intent,
)
from binkeeper.personal_memory import CaptureRequest, PersonalMemoryError, PersonalMemoryService

PrintStatus = Literal["unresolved", "submitted", "failed", "unknown"]
OUTCOME_KIND = "bin_label_print_outcome"


@dataclass(frozen=True)
class PrintRecord:
    action_id: str
    requested_at: datetime
    target: str
    copies: int | None
    status: PrintStatus
    error: str | None = None
    replayed: bool = False
    reserved: bool = True

    @property
    def display_target(self) -> str:
        return self.target.partition(":")[2] or self.target


def submit_label(
    bin_code: str,
    action_id: str,
    job: bin_label.LabelJob,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
    requested_at: datetime | None = None,
) -> PrintRecord:
    """Commit an exact reservation, attempt once, then append the observed result.

    A crash with no outcome leaves an unresolved reservation. Neither restart nor
    a replay may claim that reservation for another physical attempt.
    """
    from binkeeper.db import connect

    try:
        action_id = str(UUID(action_id))
    except ValueError as exc:
        raise bin_label.BinLabelError("The print request needs a valid action identity.") from exc
    when = requested_at or datetime.now(UTC)
    intent = BinLabelPrintIntent(
        bin_code,
        action_id,
        when,
        job.target,
        job.format,
        hashlib.sha256(job.payload).hexdigest(),
        job.copies,
    )
    try:
        with connect() as conn, conn.transaction():
            reservation = reserve_label_print_intent(conn, intent, scope=scope)
            if reservation.already_existed:
                record = next(
                    r
                    for r in print_history(conn, bin_code, scope=scope)
                    if r.action_id == action_id
                )
                return replace(record, replayed=True)
    except (psycopg.Error, PersonalMemoryError, BinManageError) as exc:
        raise bin_label.BinLabelError(
            "Could not reserve the print request. No label was submitted."
        ) from exc

    status: PrintStatus = "submitted"
    error = None
    try:
        bin_label.send_label_job(job)
    except bin_label.BinLabelError as exc:
        status = (
            "unknown"
            if isinstance(exc, B1PrintUnknown) or isinstance(exc.__cause__, TimeoutExpired)
            else "failed"
        )
        error = str(exc)
    try:
        with connect() as conn, conn.transaction():
            _record_outcome(conn, intent, status, error, scope)
    except (psycopg.Error, PersonalMemoryError):
        logging.getLogger(__name__).exception("Could not record label handoff outcome")
        status = "unknown"
        error = (
            "The printer result could not be recorded. "
            "Check the printer before requesting another label."
        )
    return PrintRecord(action_id, when, job.target, job.copies, status, error)


def _record_outcome(
    conn: psycopg.Connection,
    intent: BinLabelPrintIntent,
    status: PrintStatus,
    error: str | None,
    scope: BinActionScope,
) -> None:
    when = datetime.now(UTC)
    PersonalMemoryService(conn).capture(
        CaptureRequest(
            text=f"Label handoff for {intent.bin_code}: {status}",
            capture_type="observation",
            privacy_tier=1,
            observed_at=when,
            source_label="binkeeper-print",
            idempotency_key=f"binprint-outcome:{intent.bin_code}:{intent.action_id}",
            metadata={
                "kind": OUTCOME_KIND,
                "schema_version": "bin_label_print_outcome.v1",
                "bin_code": intent.bin_code,
                "intent_id": intent.action_id,
                "status": status,
                "error": error,
                "observed_at": when.isoformat(),
            },
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )
    )


def print_history(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> list[PrintRecord]:
    """Fold known requests and observed outcomes; missing outcomes stay unresolved.

    Legacy v1/v2 intents and creation requests without a separate reservation
    are reported honestly, without inventing successful historical handoffs.
    """
    rows = conn.execute(
        """SELECT raw_payload->'metadata', observed_at FROM captures
           WHERE tenant_id = %s AND corpus_id = %s
             AND raw_payload->'metadata'->>'bin_code' = %s
             AND raw_payload->'metadata'->>'kind' IN
                 ('bin_label_print_intent', 'bin_label_print_outcome', 'bin_capture')
           ORDER BY observed_at, id""",
        (scope.tenant_id, scope.corpus_id, bin_code),
    ).fetchall()
    records: dict[str, PrintRecord] = {}
    outcomes: dict[str, tuple[PrintStatus, str | None]] = {}
    for metadata, observed_at in rows:
        kind = metadata.get("kind")
        if kind == OUTCOME_KIND:
            outcomes[metadata["intent_id"]] = (metadata["status"], metadata.get("error"))
        elif kind == "bin_label_print_intent":
            action = metadata["intent_id"]
            records[action] = PrintRecord(
                action,
                observed_at,
                metadata.get("target") or "Unrecorded printer",
                metadata.get("copies"),
                "unresolved",
            )
        else:
            request = metadata.get("creation_request") or metadata.get("quick_label")
            if isinstance(request, dict) and request.get("copies", 0) > 0:
                action = request["action_id"]
                records.setdefault(
                    action,
                    PrintRecord(
                        action,
                        observed_at,
                        request["printer"],
                        request["copies"],
                        "unresolved",
                        reserved=False,
                    ),
                )
    for action, (status, error) in outcomes.items():
        if action in records:
            records[action] = replace(records[action], status=status, error=error)
    return sorted(
        records.values(), key=lambda record: (record.requested_at, record.action_id), reverse=True
    )
