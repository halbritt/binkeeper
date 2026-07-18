"""Narrow capture seam used by registration; no general memory dependency."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb


class PersonalCaptureConflict(RuntimeError):
    """An idempotency key exists with different capture evidence."""


@dataclass(frozen=True)
class CaptureRequest:
    text: str
    capture_type: str
    privacy_tier: int
    observed_at: datetime
    source_label: str
    idempotency_key: str
    metadata: dict[str, object]
    tenant_id: str
    corpus_id: str


@dataclass(frozen=True)
class CaptureResult:
    capture_id: str
    already_existed: bool


class PersonalMemoryService:
    """Append registration evidence to the standalone capture ledger."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def capture(self, request: CaptureRequest) -> CaptureResult:
        external_id = "capture:" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()
        payload = {
            "text": request.text,
            "capture_type": request.capture_type,
            "metadata": request.metadata,
            "idempotency_key": request.idempotency_key,
        }
        row = self._conn.execute(
            "SELECT id::text, payload FROM capture_evidence WHERE external_id = %s",
            (external_id,),
        ).fetchone()
        if row is not None:
            if _canonical(row[1]) != _canonical(payload):
                raise PersonalCaptureConflict(external_id)
            return CaptureResult(str(row[0]), True)
        inserted = self._conn.execute(
            """
            INSERT INTO capture_evidence (
                external_id, captured_at, content_text, payload, privacy_class, provenance
            ) VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id::text
            """,
            (
                external_id,
                request.observed_at,
                request.text,
                Jsonb(payload),
                f"tier-{request.privacy_tier}",
                Jsonb(
                    {
                        "source_label": request.source_label,
                        "tenant_id": request.tenant_id,
                        "corpus_id": request.corpus_id,
                    }
                ),
            ),
        ).fetchone()
        assert inserted is not None
        return CaptureResult(str(inserted[0]), False)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
