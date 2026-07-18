"""Mutable current-bin actions projected from append-only evidence.

Profile edits append complete snapshots, photo additions append capture links,
and location changes remain move-ledger events. Reprints reserve a durable
intent before the physical CUPS handoff in the web layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

import psycopg

from binkeeper.bin_inventory import (
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    RecordResult,
    record_event,
)
from binkeeper.bin_passport import BIN_CAPTURE_KIND, load_known_bin_codes
from binkeeper.personal_memory import CaptureRequest, CaptureResult, PersonalMemoryService

BIN_CAPTURE_SCHEMA_VERSION: Final[str] = "bin_capture.v1"
BIN_MANAGE_SOURCE_LABEL: Final[str] = "binkeeper-manage"
BIN_LABEL_PRINT_INTENT_KIND: Final[str] = "bin_label_print_intent"
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class BinManageError(Exception):
    """Domain root for reviewed existing-bin action failures."""


class BinManageNotFoundError(BinManageError):
    """Raised when an action targets a bin outside the selected corpus."""


@dataclass(frozen=True)
class BinActionScope:
    """Tenant and corpus that own one existing-bin action."""

    tenant_id: str = DEFAULT_BIN_TENANT_ID
    corpus_id: str = DEFAULT_BIN_CORPUS_ID


DEFAULT_BIN_ACTION_SCOPE: Final[BinActionScope] = BinActionScope()


@dataclass(frozen=True)
class BinProfileUpdate:
    """One complete owner-reviewed current-profile snapshot."""

    bin_code: str
    theme: str
    contents: str
    home_site: str
    action_id: str
    observed_at: datetime


@dataclass(frozen=True)
class BinPhotoAddition:
    """One owner-added photo link for an existing bin."""

    bin_code: str
    photo_sha256: str
    action_id: str
    observed_at: datetime


@dataclass(frozen=True)
class BinPlacement:
    """One explicit current-location placement for an existing bin."""

    bin_code: str
    site: str
    action_id: str
    occurred_at: datetime


@dataclass(frozen=True)
class BinLabelPrintIntent:
    """One owner-requested, single-attempt label reprint."""

    bin_code: str
    action_id: str
    requested_at: datetime
    queue: str
    tspl_sha256: str


def update_bin_profile(
    conn: psycopg.Connection,
    update: BinProfileUpdate,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> CaptureResult:
    """Append a full profile snapshot; a replay of one action is idempotent."""
    code, action_id = _existing_bin_action(conn, update.bin_code, update.action_id, scope)
    idempotency_key = f"binprofile:{code}:{action_id}"
    observed_at = _stable_capture_time(conn, idempotency_key, update.observed_at, scope)
    request = _scoped_capture(
        _profile_capture_request(code, update, observed_at, idempotency_key),
        scope,
    )
    return PersonalMemoryService(conn).capture(request)


def append_bin_photo(
    conn: psycopg.Connection,
    addition: BinPhotoAddition,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> CaptureResult:
    """Append one capture-linked photo without changing current profile fields."""
    code, action_id = _existing_bin_action(conn, addition.bin_code, addition.action_id, scope)
    digest = _sha256(addition.photo_sha256, "photo_sha256")
    idempotency_key = f"binphoto:{code}:{action_id}"
    observed_at = _stable_capture_time(conn, idempotency_key, addition.observed_at, scope)
    request = _scoped_capture(
        _photo_capture_request(code, digest, observed_at, idempotency_key),
        scope,
    )
    return PersonalMemoryService(conn).capture(request)


def place_existing_bin(
    conn: psycopg.Connection,
    placement: BinPlacement,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> RecordResult:
    """Append an explicit current-location placement for an existing bin."""
    code, action_id = _existing_bin_action(
        conn,
        placement.bin_code,
        placement.action_id,
        scope,
    )
    site = placement.site.strip()
    if not site:
        raise BinManageError("site is required")
    idempotency_key = f"binplace:{code}:{action_id}"
    _validate_placement_replay(conn, idempotency_key, site, scope)
    result = record_event(
        conn,
        event_kind="place",
        bin_code=code,
        site=site,
        occurred_at=_aware_datetime(placement.occurred_at, "occurred_at"),
        source_label=BIN_MANAGE_SOURCE_LABEL,
        idempotency_key=idempotency_key,
        metadata={"binkeeper_manage": True},
        tenant_id=scope.tenant_id,
        corpus_id=scope.corpus_id,
    )
    if result.already_existed:
        _validate_placement_replay(conn, idempotency_key, site, scope)
    return result


def reserve_label_print_intent(
    conn: psycopg.Connection,
    intent: BinLabelPrintIntent,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> CaptureResult:
    """Append one immutable print reservation; an exact replay is a no-op."""
    code, action_id = _existing_bin_action(conn, intent.bin_code, intent.action_id, scope)
    queue = intent.queue.strip()
    if not queue:
        raise BinManageError("queue is required")
    digest = _sha256(intent.tspl_sha256, "tspl_sha256")
    idempotency_key = f"binprint:{code}:{action_id}"
    requested_at = _stable_capture_time(conn, idempotency_key, intent.requested_at, scope)
    normalized_intent = replace(
        intent,
        action_id=action_id,
        requested_at=requested_at,
        queue=queue,
        tspl_sha256=digest,
    )
    request = _scoped_capture(
        _print_capture_request(code, normalized_intent, idempotency_key),
        scope,
    )
    return PersonalMemoryService(conn).capture(request)


def _profile_capture_request(
    code: str,
    update: BinProfileUpdate,
    observed_at: datetime,
    idempotency_key: str,
) -> CaptureRequest:
    theme = update.theme.strip()
    contents = update.contents.strip()
    home_site = update.home_site.strip()
    metadata: dict[str, object] = {
        "kind": BIN_CAPTURE_KIND,
        "schema_version": BIN_CAPTURE_SCHEMA_VERSION,
        "bin_code": code,
        "captured_at": observed_at.isoformat(),
        "app": "binkeeper-bin-manage/0.1",
        "profile_mode": "snapshot",
        "profile_fields": ["theme", "home_site", "contents_text"],
        "contents_text": contents,
        "bin_profile": {"theme": theme, "home_site": home_site},
    }
    text = f"bin {code} profile · {contents}" if contents else f"bin {code} profile updated"
    return _capture_request(text, metadata, observed_at, idempotency_key)


def _photo_capture_request(
    code: str,
    digest: str,
    observed_at: datetime,
    idempotency_key: str,
) -> CaptureRequest:
    metadata: dict[str, object] = {
        "kind": BIN_CAPTURE_KIND,
        "schema_version": BIN_CAPTURE_SCHEMA_VERSION,
        "bin_code": code,
        "captured_at": observed_at.isoformat(),
        "app": "binkeeper-bin-manage/0.1",
        "contents_text": "",
        "photo": {"sha256": digest, "blob_ref": digest},
    }
    return _capture_request(f"bin {code} photo added", metadata, observed_at, idempotency_key)


def _print_capture_request(
    code: str,
    intent: BinLabelPrintIntent,
    idempotency_key: str,
) -> CaptureRequest:
    metadata: dict[str, object] = {
        "kind": BIN_LABEL_PRINT_INTENT_KIND,
        "schema_version": "bin_label_print_intent.v1",
        "bin_code": code,
        "intent_id": intent.action_id,
        "requested_at": intent.requested_at.isoformat(),
        "queue": intent.queue,
        "tspl_sha256": intent.tspl_sha256,
        "app": "binkeeper-bin-manage/0.1",
    }
    return _capture_request(
        f"BinKeeper label print requested for {code}",
        metadata,
        intent.requested_at,
        idempotency_key,
    )


def _capture_request(
    text: str,
    metadata: dict[str, object],
    observed_at: datetime,
    idempotency_key: str,
) -> CaptureRequest:
    return CaptureRequest(
        text=text,
        capture_type="observation",
        privacy_tier=1,
        observed_at=observed_at,
        source_label=BIN_MANAGE_SOURCE_LABEL,
        idempotency_key=idempotency_key,
        metadata=metadata,
    )


def _scoped_capture(request: CaptureRequest, scope: BinActionScope) -> CaptureRequest:
    return replace(request, tenant_id=scope.tenant_id, corpus_id=scope.corpus_id)


def _existing_bin_action(
    conn: psycopg.Connection,
    bin_code: str,
    action_id: str,
    scope: BinActionScope,
) -> tuple[str, str]:
    code = bin_code.strip()
    if not code:
        raise BinManageNotFoundError("bin code is required")
    known = load_known_bin_codes(
        conn,
        tenant_id=scope.tenant_id,
        corpus_id=scope.corpus_id,
    )
    if code not in known:
        raise BinManageNotFoundError(f"unknown bin {code!r}")
    return code, _action_id(action_id)


def _stable_capture_time(
    conn: psycopg.Connection,
    idempotency_key: str,
    proposed_at: datetime,
    scope: BinActionScope,
) -> datetime:
    """Use server receipt time on first write and the stored time on replay."""
    row = conn.execute(
        """
        SELECT observed_at
        FROM captures
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND raw_payload->>'source_label' = %s
          AND raw_payload->>'idempotency_key' = %s
        LIMIT 1
        """,
        (scope.tenant_id, scope.corpus_id, BIN_MANAGE_SOURCE_LABEL, idempotency_key),
    ).fetchone()
    if row is None:
        return _aware_datetime(proposed_at, "action time")
    if not isinstance(row[0], datetime):
        raise BinManageError("existing action has an invalid timestamp")
    return _aware_datetime(row[0], "existing action time")


def _validate_placement_replay(
    conn: psycopg.Connection,
    idempotency_key: str,
    site: str,
    scope: BinActionScope,
) -> None:
    row = conn.execute(
        """
        SELECT site
        FROM bin_trip_events
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND source_label = %s
          AND raw_payload->>'idempotency_key' = %s
        LIMIT 1
        """,
        (scope.tenant_id, scope.corpus_id, BIN_MANAGE_SOURCE_LABEL, idempotency_key),
    ).fetchone()
    if row is not None and row[0] != site:
        raise BinManageError("placement action was reused with a different site")


def _action_id(value: str) -> str:
    """Normalize a server-rendered UUID used as the idempotency identity."""
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise BinManageError("action_id must be a UUID") from exc


def _sha256(value: str, field_name: str) -> str:
    digest = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise BinManageError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BinManageError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)
