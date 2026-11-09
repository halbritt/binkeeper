"""Create one new bin atomically from a stable, owner-reviewed action."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

import psycopg

from binkeeper.bin_geo import normalize_bin_code
from binkeeper.bin_inventory import record_event
from binkeeper.bin_manage import (
    DEFAULT_BIN_ACTION_SCOPE,
    BinActionScope,
    append_saved_bin_photos,
)
from binkeeper.bin_passport import load_known_bin_codes
from binkeeper.bin_register import BIN_CAPTURE_SCHEMA_VERSION, RegisterResult
from binkeeper.bin_saved_photos import MAX_LABEL_PHOTOS
from binkeeper.personal_memory import CaptureRequest, PersonalMemoryService, capture_external_id
from binkeeper.sites import SITE_PREFIXES


class BinCreationError(ValueError):
    """The reviewed request cannot safely create a new bin."""


class BinCreationConflict(BinCreationError):
    """An action identity has already been used with different details."""


@dataclass(frozen=True)
class CreateBinRequest:
    action_id: str
    bin_code: str
    site: str
    theme: str
    contents: str
    photo_refs: tuple[str, ...] = ()
    printer: str = "cups"
    copies: int = 0


def _reviewed(request: CreateBinRequest) -> CreateBinRequest:
    try:
        action_id = str(UUID(request.action_id))
    except (ValueError, AttributeError) as exc:
        raise BinCreationError("Review this request again before creating the bin.") from exc
    reviewed = replace(
        request,
        action_id=action_id,
        bin_code=request.bin_code.strip().upper(),
        site=request.site.strip(),
        theme=request.theme.strip(),
        contents=request.contents.strip(),
        photo_refs=tuple(dict.fromkeys(ref.strip().lower() for ref in request.photo_refs)),
    )
    if reviewed.site not in SITE_PREFIXES:
        raise BinCreationError("Choose a site for the new bin.")
    if reviewed.bin_code and normalize_bin_code(reviewed.bin_code) != reviewed.bin_code:
        raise BinCreationError("Enter a bin code such as AGR-014, or leave it blank to assign one.")
    if not reviewed.theme or len(reviewed.theme) > 120:
        raise BinCreationError("Enter a label of at most 120 characters.")
    if len(reviewed.contents) > 1200:
        raise BinCreationError("Keep contents to at most 1200 characters.")
    if len(reviewed.photo_refs) > MAX_LABEL_PHOTOS:
        raise BinCreationError(f"Choose at most {MAX_LABEL_PHOTOS} photos.")
    if reviewed.printer not in {"cups", "niimbot-b1"} or reviewed.copies not in {0, 1, 2}:
        raise BinCreationError("Choose an available printer and one or two labels.")
    return reviewed


def create_bin(
    conn: psycopg.Connection,
    request: CreateBinRequest,
    *,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
    observed_at: datetime | None = None,
) -> RegisterResult:
    """Commit the action receipt, initial placement and every photo link together.

    Physical-label observations keep their separate day-scoped registration
    contract. A creation replay never places, links or prints again.
    """
    request = _reviewed(request)
    payload = {**asdict(request), "photo_refs": list(request.photo_refs)}
    identity = f"bin-create:{request.action_id}"
    external_id = capture_external_id(
        idempotency_key=identity, tenant_id=scope.tenant_id, corpus_id=scope.corpus_id
    )
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (external_id,))
        existing = conn.execute(
            "SELECT id::text, payload->'metadata' FROM capture_evidence WHERE external_id = %s",
            (external_id,),
        ).fetchone()
        if existing is not None:
            metadata = existing[1]
            if metadata.get("creation_request") != payload:
                raise BinCreationConflict(
                    "This creation request was already used with different details."
                )
            return RegisterResult(metadata["bin_code"], metadata["site"], existing[0], "", True)

        prefix = (
            request.bin_code.split("-", 1)[0] if request.bin_code else SITE_PREFIXES[request.site]
        )
        # Quick-label allocation uses this same lock; neither path can claim the
        # other's code between its availability check and evidence commit.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"bin-code:{prefix}",)
        )
        known = load_known_bin_codes(conn, tenant_id=scope.tenant_id, corpus_id=scope.corpus_id)
        code = request.bin_code or next_bin_code(prefix, known)
        if code in known:
            raise BinCreationError(
                f"Bin code {code} is already in use. "
                "Choose another code, or clear it to assign one."
            )
        when = observed_at or datetime.now(UTC)
        metadata = {
            "kind": "bin_capture",
            "schema_version": BIN_CAPTURE_SCHEMA_VERSION,
            "bin_code": code,
            "site": request.site,
            "captured_at": when.isoformat(),
            "bin_profile": {"theme": request.theme},
            "contents_text": request.contents,
            "creation_request": payload,
            "app": "binkeeper-bin-create/1",
        }
        capture = PersonalMemoryService(conn).capture(
            CaptureRequest(
                text=f"bin {code} @ {request.site} · {request.contents}",
                capture_type="observation",
                privacy_tier=1,
                observed_at=when,
                source_label=identity,
                idempotency_key=identity,
                metadata=metadata,
                tenant_id=scope.tenant_id,
                corpus_id=scope.corpus_id,
            )
        )
        place = record_event(
            conn,
            event_kind="place",
            bin_code=code,
            site=request.site,
            occurred_at=when,
            source_label=identity,
            idempotency_key=identity,
            metadata={"creation_action_id": request.action_id},
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )
        if request.photo_refs:
            append_saved_bin_photos(conn, code, request.photo_refs, scope=scope)
        return RegisterResult(code, request.site, capture.capture_id, place.event_id, False)


def next_bin_code(prefix: str, known_codes: list[str]) -> str:
    highest = 0
    for code in known_codes:
        if code.startswith(f"{prefix}-"):
            suffix = code[len(prefix) + 1 :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"
