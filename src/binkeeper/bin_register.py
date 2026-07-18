"""Register a labeled bin at its GPS-resolved site (RFC 0088 registration tap).

Registration writes two append-only records on the owner connection:

* a ``bin_capture`` capture (``raw_payload.metadata`` per ``bin_capture.v1``) --
  the discovery + evidence record, carrying the site, the photo blob ref, the
  contents, and the GPS fix (precise location stays inside metadata, never a
  column); and
* a ``place`` move event (``bin_trip_events``) -- the authoritative location, so
  ``bin_where`` / ``bin_belief`` resolve the bin to the site.

Nothing is guessed: the caller resolves the site (abstaining to an owner pick on
ambiguity) before calling here. Location writes need the owner role; a serving
connection is rejected by the ledgers' grants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import psycopg

from binkeeper.bin_geo import GpsFix
from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID, record_event
from binkeeper.personal_memory import (
    CaptureRequest,
    PersonalCaptureConflict,
    PersonalMemoryService,
)

BIN_CAPTURE_KIND: Final[str] = "bin_capture"
BIN_CAPTURE_SCHEMA_VERSION: Final[str] = "bin_capture.v1"
REGISTRATION_SOURCE_LABEL: Final[str] = "photo-registration"


class BinRegisterError(Exception):
    """Domain root for registration failures (RFC 0012 per-stage family)."""


@dataclass(frozen=True)
class RegisterResult:
    """Handles for the two records a registration writes."""

    bin_code: str
    site: str
    capture_id: str
    trip_event_id: str
    already_existed: bool


def register_bin(
    conn: psycopg.Connection,
    *,
    bin_code: str,
    site: str,
    gps: GpsFix | None = None,
    photo_sha256: str | None = None,
    contents_text: str | None = None,
    theme: str | None = None,
    observed_at: datetime | None = None,
    source_label: str = REGISTRATION_SOURCE_LABEL,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> RegisterResult:
    """Write the bin_capture + place event for a labeled bin. Idempotent per day."""
    code = bin_code.strip()
    clean_site = site.strip()
    if not code:
        raise BinRegisterError("register_bin requires a non-empty bin_code")
    if not clean_site:
        raise BinRegisterError("register_bin requires a non-empty site")
    when = observed_at or datetime.now(UTC)

    metadata: dict[str, object] = {
        "kind": BIN_CAPTURE_KIND,
        "schema_version": BIN_CAPTURE_SCHEMA_VERSION,
        "bin_code": code,
        "site": clean_site,
        "captured_at": when.isoformat(),
        "app": "engram-bin-register/0.1",
    }
    if gps is not None:
        metadata["gps"] = {"lat": gps.lat, "lon": gps.lon, "accuracy_m": gps.accuracy_m}
    if photo_sha256:
        metadata["photo"] = {"sha256": photo_sha256, "blob_ref": photo_sha256}
    clean_contents = (contents_text or "").strip()
    if clean_contents:
        metadata["contents_text"] = clean_contents
    clean_theme = (theme or "").strip()
    if clean_theme:
        metadata["bin_profile"] = {"theme": clean_theme}

    text = f"bin {code} @ {clean_site}" + (f" · {clean_contents}" if clean_contents else "")
    # Idempotent within a day: a double-submit dedupes, a genuine re-place on a
    # later day (or a different site) appends a fresh record.
    idem = f"binreg:{code}:{clean_site}:{when.date().isoformat()}"

    try:
        capture = PersonalMemoryService(conn).capture(
            CaptureRequest(
                text=text,
                capture_type="observation",
                privacy_tier=1,
                observed_at=when,
                source_label=source_label,
                idempotency_key=idem,
                metadata=metadata,
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
        )
    except PersonalCaptureConflict:
        # Same bin+site already registered today (only the sub-second timestamp
        # differs): a re-tap is a no-op, not an error. The first registration's
        # place event already stands, so don't append a duplicate.
        return RegisterResult(
            bin_code=code, site=clean_site, capture_id="", trip_event_id="", already_existed=True
        )
    if capture.already_existed:
        return RegisterResult(
            bin_code=code,
            site=clean_site,
            capture_id=capture.capture_id,
            trip_event_id="",
            already_existed=True,
        )
    place = record_event(
        conn,
        event_kind="place",
        bin_code=code,
        site=clean_site,
        occurred_at=when,
        source_label=source_label,
        idempotency_key=idem,
        metadata={"registration": True, "has_gps": gps is not None},
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    return RegisterResult(
        bin_code=code,
        site=clean_site,
        capture_id=capture.capture_id,
        trip_event_id=place.event_id,
        already_existed=False,
    )
