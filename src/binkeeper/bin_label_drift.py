"""Nightly advisory label-drift proposals and their rebuildable review queue."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from uuid import UUID

import psycopg

from binkeeper.bin_inventory import DEFAULT_BIN_CORPUS_ID, DEFAULT_BIN_TENANT_ID
from binkeeper.bin_passport import BIN_CAPTURE_KIND, BinPassport, load_bin_passports
from binkeeper.bin_vision import (
    DEFAULT_OPENROUTER_ENDPOINT,
    DEFAULT_VISION_ENDPOINT,
    DEFAULT_VISION_MODEL,
    BinLabelProposal,
    BinVisionError,
    DetectedItem,
    EnsembleVisionClient,
    OllamaVisionClient,
    VisionClient,
)
from binkeeper.personal_memory import (
    CaptureRequest,
    CaptureResult,
    PersonalMemoryService,
    capture_external_id,
)

LABEL_DRIFT_PROPOSAL_KIND: Final[str] = "label_drift_proposal"
LABEL_DRIFT_DISMISSAL_KIND: Final[str] = "label_drift_dismissal"
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+", re.IGNORECASE)
LABEL_DRIFT_SOURCE_LABEL: Final[str] = "binkeeper-label-drift"
DEFAULT_LABEL_DRIFT_MODE: Final[str] = os.environ.get("BINKEEPER_BIN_LABEL_DRIFT_MODE", "ensemble")
DEFAULT_LABEL_DRIFT_CLOUD_MODEL: Final[str] = os.environ.get(
    "BINKEEPER_BIN_LABEL_DRIFT_CLOUD_MODEL", "anthropic/claude-opus-5"
)
DEFAULT_LABEL_DRIFT_CLOUD_ENDPOINT: Final[str] = os.environ.get(
    "BINKEEPER_BIN_LABEL_DRIFT_CLOUD_ENDPOINT", DEFAULT_OPENROUTER_ENDPOINT
)
DEFAULT_LABEL_DRIFT_CLOUD_TIMEOUT_S: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_LABEL_DRIFT_CLOUD_TIMEOUT_S", "120")
)
DEFAULT_LABEL_DRIFT_LOCAL_TIMEOUT_S: Final[float] = float(
    os.environ.get("BINKEEPER_BIN_LABEL_DRIFT_LOCAL_TIMEOUT_S", "240")
)


class LabelDriftError(RuntimeError):
    """Raised when the durable label-drift contract cannot be satisfied."""


class PhotoOpener(Protocol):
    """Read one owner-local photo from the vault by plaintext digest."""

    def __call__(self, conn: psycopg.Connection, sha256: str) -> bytes | None:
        """Return plaintext photo bytes, or None when the evidence cannot be read."""
        ...


@dataclass(frozen=True)
class LabelDriftPassportSnapshot:
    """Passport fields that affect the proposal, diff, and input identity."""

    bin_code: str
    theme: str | None
    owner_phrase: str | None
    accepts: tuple[str, ...]
    contents: str | None
    projector_version: str

    def to_json(self) -> dict[str, object]:
        return {
            "bin_code": self.bin_code,
            "theme": self.theme,
            "owner_phrase": self.owner_phrase,
            "accepts": list(self.accepts),
            "contents": self.contents,
            "projector_version": self.projector_version,
        }


@dataclass
class LabelDriftHarvestSummary:
    """Journal-safe aggregate for one incremental nightly pass."""

    bins_scanned: int = 0
    bins_analyzed: int = 0
    proposals_recorded: int = 0
    material_proposals: int = 0
    skipped_unchanged: int = 0
    skipped_no_photos: int = 0
    skipped_unreadable_photos: int = 0
    model_errors: int = 0

    def to_json(self) -> dict[str, object]:
        return {
            "bins_scanned": self.bins_scanned,
            "bins_analyzed": self.bins_analyzed,
            "proposals_recorded": self.proposals_recorded,
            "material_proposals": self.material_proposals,
            "skipped_unchanged": self.skipped_unchanged,
            "skipped_no_photos": self.skipped_no_photos,
            "skipped_unreadable_photos": self.skipped_unreadable_photos,
            "model_errors": self.model_errors,
        }


@dataclass(frozen=True)
class LabelDriftEvidence:
    """One proposal, dismissal, or profile event consumed by the queue fold."""

    external_id: str
    bin_code: str
    kind: str
    observed_at: datetime
    metadata: Mapping[str, object]


@dataclass(frozen=True)
class LabelDriftQueueEntry:
    """One current material proposal awaiting owner review."""

    proposal_external_id: str
    bin_code: str
    proposed_at: datetime
    proposed_theme: str
    current_theme: str | None
    current_contents: str | None
    new_item_labels: tuple[str, ...]
    photo_hashes: tuple[str, ...]
    model_versions: tuple[str, ...]

    @property
    def proposed_contents(self) -> str:
        values = [self.current_contents or "", *self.new_item_labels]
        return ", ".join(dict.fromkeys(value.strip() for value in values if value.strip()))


@dataclass(frozen=True)
class LabelDriftDiff:
    """The candidate changes that determine whether owner review is warranted."""

    theme_before: str | None
    theme_after: str
    theme_changed: bool
    new_items: tuple[DetectedItem, ...]

    @property
    def new_item_labels(self) -> tuple[str, ...]:
        return tuple(item.label for item in self.new_items)

    @property
    def material(self) -> bool:
        return self.theme_changed or len(self.new_items) >= 2

    def to_json(self) -> dict[str, object]:
        reasons: list[str] = []
        if self.theme_changed:
            reasons.append("theme_changed")
        if len(self.new_items) >= 2:
            reasons.append("two_or_more_new_items")
        return {
            "theme": {
                "before": self.theme_before,
                "after": self.theme_after,
                "changed": self.theme_changed,
            },
            "new_items": [item.to_json() for item in self.new_items],
            "material": self.material,
            "material_reasons": reasons,
        }


def default_label_drift_client(
    *,
    mode: str | None = None,
    local_endpoint: str | None = None,
    local_model: str | None = None,
) -> VisionClient:
    """Build the ADR 0006 ensemble, or its configured local-only rollback."""
    selected_mode = (mode or DEFAULT_LABEL_DRIFT_MODE).strip().lower()
    local = OllamaVisionClient(
        endpoint=local_endpoint or DEFAULT_VISION_ENDPOINT,
        model=local_model or DEFAULT_VISION_MODEL,
        timeout_s=DEFAULT_LABEL_DRIFT_LOCAL_TIMEOUT_S,
    )
    if selected_mode == "local":
        return local
    if selected_mode != "ensemble":
        raise BinVisionError(
            f"unsupported label-drift mode {selected_mode!r} (use 'ensemble' or 'local')"
        )
    key = os.environ.get("BINKEEPER_OPENROUTER_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key or not key.strip():
        raise BinVisionError("no OpenRouter API key configured for the label-drift cloud leg")
    cloud = OllamaVisionClient(
        endpoint=DEFAULT_LABEL_DRIFT_CLOUD_ENDPOINT,
        model=DEFAULT_LABEL_DRIFT_CLOUD_MODEL,
        api_key=key.strip(),
        timeout_s=DEFAULT_LABEL_DRIFT_CLOUD_TIMEOUT_S,
    )
    return EnsembleVisionClient(cloud=cloud, local=local)


def diff_label_proposal(passport: BinPassport, proposal: BinLabelProposal) -> LabelDriftDiff:
    """Compare one proposal with the current passport using ADR 0006 materiality."""
    theme_changed = _normalize(proposal.theme) != _normalize(passport.theme or "")
    current_contents = _normalize(
        " ".join(
            (*passport.accepts, passport.sibling_contents[-1] if passport.sibling_contents else "")
        )
    )
    new_items = tuple(
        item for item in proposal.items if _normalize(item.label) not in current_contents
    )
    return LabelDriftDiff(
        theme_before=passport.theme,
        theme_after=proposal.theme,
        theme_changed=theme_changed,
        new_items=new_items,
    )


def fold_label_drift_queue(
    evidence: Sequence[LabelDriftEvidence],
    *,
    now: datetime | None = None,
) -> list[LabelDriftQueueEntry]:
    """Fold append-only drift evidence into at most one pending entry per bin."""
    effective_now = now or datetime.now(UTC)
    proposals: dict[str, LabelDriftEvidence] = {}
    profile_snapshots: dict[str, LabelDriftEvidence] = {}
    dismissals: defaultdict[str, list[LabelDriftEvidence]] = defaultdict(list)
    for event in sorted(evidence, key=lambda row: (row.observed_at, row.external_id)):
        if event.kind == LABEL_DRIFT_PROPOSAL_KIND:
            proposals[event.bin_code] = event
        elif event.kind == BIN_CAPTURE_KIND and event.metadata.get("profile_mode") == "snapshot":
            profile_snapshots[event.bin_code] = event
        elif event.kind == LABEL_DRIFT_DISMISSAL_KIND:
            dismissals[event.bin_code].append(event)
    entries: list[LabelDriftQueueEntry] = []
    for bin_code, proposal in proposals.items():
        profile = profile_snapshots.get(bin_code)
        if profile is not None and _evidence_key(profile) > _evidence_key(proposal):
            continue
        diff = _mapping(proposal.metadata.get("diff"))
        proposed = _mapping(proposal.metadata.get("proposal"))
        snapshot = _mapping(proposal.metadata.get("passport_snapshot"))
        if not diff or not proposed or not snapshot or diff.get("material") is not True:
            continue
        proposed_theme = _text(proposed.get("theme"))
        if not proposed_theme:
            continue
        new_items = _mappings(diff.get("new_items"))
        photo_hashes = _strings(proposal.metadata.get("photo_hashes"))
        dismissed_themes: set[str] = set()
        dismissed_items: set[str] = set()
        for dismissal in dismissals[bin_code]:
            age = effective_now - dismissal.observed_at
            if age < timedelta(0) or age >= timedelta(days=90):
                continue
            if _strings(dismissal.metadata.get("photo_hashes")) != photo_hashes:
                continue
            if dismissed_theme := _text(dismissal.metadata.get("dismissed_theme")):
                dismissed_themes.add(_normalize(dismissed_theme))
            dismissed_items.update(
                _normalize(label) for label in _strings(dismissal.metadata.get("dismissed_items"))
            )
        theme = _mapping(diff.get("theme")) or {}
        theme_changed = (
            theme.get("changed") is True and _normalize(proposed_theme) not in dismissed_themes
        )
        effective_new_items = tuple(
            item
            for item in new_items
            if (label := _text(item.get("label"))) is not None
            and _normalize(label) not in dismissed_items
        )
        if not theme_changed and len(effective_new_items) < 2:
            continue
        current_theme = _text(snapshot.get("theme"))
        entries.append(
            LabelDriftQueueEntry(
                proposal_external_id=proposal.external_id,
                bin_code=bin_code,
                proposed_at=proposal.observed_at,
                proposed_theme=proposed_theme if theme_changed else current_theme or proposed_theme,
                current_theme=current_theme,
                current_contents=_text(snapshot.get("contents")),
                new_item_labels=tuple(
                    label
                    for item in effective_new_items
                    if (label := _text(item.get("label"))) is not None
                ),
                photo_hashes=photo_hashes,
                model_versions=_strings(proposal.metadata.get("model_versions")),
            )
        )
    return sorted(entries, key=lambda entry: (-entry.proposed_at.timestamp(), entry.bin_code))


def load_label_drift_evidence(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[LabelDriftEvidence]:
    """Load the append-only evidence needed to rebuild the current review queue."""
    rows = conn.execute(
        """
        SELECT external_id,
               raw_payload->'metadata',
               COALESCE(observed_at, imported_at)
        FROM captures
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND (
              raw_payload->'metadata'->>'kind' = ANY(%s)
              OR (
                  raw_payload->'metadata'->>'kind' = %s
                  AND raw_payload->'metadata'->>'profile_mode' = 'snapshot'
              )
          )
        ORDER BY COALESCE(observed_at, imported_at), external_id
        """,
        (
            tenant_id,
            corpus_id,
            [LABEL_DRIFT_PROPOSAL_KIND, LABEL_DRIFT_DISMISSAL_KIND],
            BIN_CAPTURE_KIND,
        ),
    ).fetchall()
    evidence: list[LabelDriftEvidence] = []
    for raw_external_id, raw_metadata, raw_observed_at in rows:
        metadata = _mapping(raw_metadata)
        if metadata is None:
            raise LabelDriftError("label-drift evidence metadata is not an object")
        external_id = _text(raw_external_id)
        bin_code = _text(metadata.get("bin_code"))
        kind = _text(metadata.get("kind"))
        if external_id is None or bin_code is None or kind is None:
            raise LabelDriftError("label-drift evidence is missing its identity")
        if not isinstance(raw_observed_at, datetime):
            raise LabelDriftError("label-drift evidence is missing its timestamp")
        evidence.append(
            LabelDriftEvidence(
                external_id=external_id,
                bin_code=bin_code,
                kind=kind,
                observed_at=_aware_datetime(raw_observed_at, "evidence timestamp"),
                metadata=metadata,
            )
        )
    return evidence


def load_label_drift_queue(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[LabelDriftQueueEntry]:
    """Rebuild the current owner review queue from immutable evidence."""
    return fold_label_drift_queue(
        load_label_drift_evidence(conn, tenant_id=tenant_id, corpus_id=corpus_id),
        now=now,
    )


def dismiss_label_drift_proposal(
    conn: psycopg.Connection,
    *,
    bin_code: str,
    proposal_external_id: str,
    action_id: str,
    observed_at: datetime,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> CaptureResult:
    """Append one owner dismissal for the exact current proposal."""
    code = _required_text(bin_code, "bin_code")
    proposal_id = _required_text(proposal_external_id, "proposal_external_id")
    normalized_action_id = _action_id(action_id)
    when = _aware_datetime(observed_at, "observed_at")
    idempotency_key = f"label-drift-dismiss:{code}:{proposal_id}:{normalized_action_id}"
    external_id = capture_external_id(
        idempotency_key=idempotency_key,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    existing = conn.execute(
        """
        SELECT id::text, raw_payload->'metadata'
        FROM captures
        WHERE tenant_id = %s AND corpus_id = %s AND external_id = %s
        """,
        (tenant_id, corpus_id, external_id),
    ).fetchone()
    if existing is not None:
        existing_metadata = _mapping(existing[1])
        if (
            existing_metadata is None
            or existing_metadata.get("kind") != LABEL_DRIFT_DISMISSAL_KIND
            or existing_metadata.get("bin_code") != code
            or existing_metadata.get("proposal_external_id") != proposal_id
        ):
            raise LabelDriftError("dismissal action_id was reused with different evidence")
        return CaptureResult(capture_id=str(existing[0]), already_existed=True)

    current = next(
        (
            entry
            for entry in load_label_drift_queue(
                conn,
                now=when,
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
            if entry.bin_code == code
        ),
        None,
    )
    if current is None or current.proposal_external_id != proposal_id:
        raise LabelDriftError("proposal is not the current pending proposal for this bin")
    dismissed_theme = (
        current.proposed_theme
        if _normalize(current.proposed_theme) != _normalize(current.current_theme or "")
        else None
    )
    metadata: dict[str, object] = {
        "kind": LABEL_DRIFT_DISMISSAL_KIND,
        "schema_version": "label_drift_dismissal.v1",
        "bin_code": code,
        "proposal_external_id": proposal_id,
        "dismissed_theme": dismissed_theme,
        "dismissed_items": list(current.new_item_labels),
        "photo_hashes": list(current.photo_hashes),
        "dismissed_at": when.isoformat(),
        "idempotency_key": idempotency_key,
        "app": "binkeeper-label-drift/0.1",
    }
    return PersonalMemoryService(conn).capture(
        CaptureRequest(
            text=f"label drift proposal dismissed for {code}",
            capture_type="observation",
            privacy_tier=1,
            observed_at=when,
            source_label=LABEL_DRIFT_SOURCE_LABEL,
            idempotency_key=idempotency_key,
            metadata=metadata,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    )


def harvest_label_drift(
    conn: psycopg.Connection,
    *,
    client: VisionClient,
    open_photo: PhotoOpener | None = None,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> LabelDriftHarvestSummary:
    """Append input-keyed label proposals for bins whose photos or passports changed."""
    when = now or datetime.now(UTC)
    opener = open_photo or _default_photo_opener()
    model_versions = _model_versions(client)
    photos_by_bin = _load_photo_hashes(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    summary = LabelDriftHarvestSummary()
    for passport in load_bin_passports(
        conn,
        now=when,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    ):
        summary.bins_scanned += 1
        photo_hashes = photos_by_bin.get(passport.bin_code, ())
        if not photo_hashes:
            summary.skipped_no_photos += 1
            continue
        snapshot = _passport_snapshot(passport)
        idempotency_key = _proposal_idempotency_key(
            passport.bin_code,
            photo_hashes=photo_hashes,
            passport=snapshot,
            model_versions=model_versions,
        )
        external_id = capture_external_id(
            idempotency_key=idempotency_key,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
        if conn.execute(
            "SELECT 1 FROM capture_evidence WHERE external_id = %s", (external_id,)
        ).fetchone():
            summary.skipped_unchanged += 1
            continue
        images = [opener(conn, digest) for digest in photo_hashes]
        if any(image is None for image in images):
            summary.skipped_unreadable_photos += 1
            continue
        from binkeeper.bin_vision import propose_bin_label

        try:
            proposal = propose_bin_label(
                client,
                [image for image in images if image is not None],
                notes=passport.owner_phrase,
                fail_on_error=True,
            )
        except BinVisionError:
            summary.model_errors += 1
            continue
        summary.bins_analyzed += 1
        diff = diff_label_proposal(passport, proposal)
        metadata: dict[str, object] = {
            "kind": LABEL_DRIFT_PROPOSAL_KIND,
            "schema_version": "label_drift_proposal.v1",
            "bin_code": passport.bin_code,
            "proposal": proposal.to_json(),
            "passport_snapshot": snapshot.to_json(),
            "diff": diff.to_json(),
            "photo_hashes": list(photo_hashes),
            "model_versions": list(model_versions),
            "idempotency_key": idempotency_key,
            "proposed_at": when.isoformat(),
            "app": "binkeeper-label-drift/0.1",
        }
        captured = PersonalMemoryService(conn).capture(
            CaptureRequest(
                text=(
                    f"label drift proposal for {passport.bin_code} "
                    f"({'material' if diff.material else 'non-material'})"
                ),
                capture_type="observation",
                privacy_tier=1,
                observed_at=when,
                source_label=LABEL_DRIFT_SOURCE_LABEL,
                idempotency_key=idempotency_key,
                metadata=metadata,
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
        )
        if captured.already_existed:
            summary.skipped_unchanged += 1
            continue
        summary.proposals_recorded += 1
        summary.material_proposals += int(diff.material)
    return summary


def _passport_snapshot(passport: BinPassport) -> LabelDriftPassportSnapshot:
    contents = passport.sibling_contents[-1] if passport.sibling_contents else None
    return LabelDriftPassportSnapshot(
        bin_code=passport.bin_code,
        theme=passport.theme,
        owner_phrase=passport.owner_phrase,
        accepts=passport.accepts,
        contents=contents,
        projector_version=passport.projector_version,
    )


def _proposal_idempotency_key(
    bin_code: str,
    *,
    photo_hashes: Sequence[str],
    passport: LabelDriftPassportSnapshot,
    model_versions: Sequence[str],
) -> str:
    inputs = {
        "schema_version": "label_drift_inputs.v1",
        "bin_code": bin_code,
        "photo_hashes": sorted(photo_hashes),
        "passport": passport.to_json(),
        "model_versions": list(model_versions),
    }
    encoded = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    return f"label-drift:{bin_code}:{hashlib.sha256(encoded).hexdigest()}"


def _model_versions(client: VisionClient) -> tuple[str, ...]:
    versions = getattr(client, "model_versions", None)
    if isinstance(versions, tuple) and all(isinstance(version, str) for version in versions):
        return versions
    return (client.model,)


def _load_photo_hashes(
    conn: psycopg.Connection,
    *,
    tenant_id: str,
    corpus_id: str,
) -> Mapping[str, tuple[str, ...]]:
    rows = conn.execute(
        """
        SELECT raw_payload->'metadata'->>'bin_code',
               COALESCE(
                   raw_payload->'metadata'->'photo'->>'sha256',
                   raw_payload->'metadata'->'photo'->>'blob_ref'
               )
        FROM captures
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND raw_payload->'metadata'->>'kind' = %s
          AND raw_payload->'metadata'->'photo' IS NOT NULL
        ORDER BY COALESCE(observed_at, imported_at), imported_at, external_id
        """,
        (tenant_id, corpus_id, BIN_CAPTURE_KIND),
    ).fetchall()
    hashes: defaultdict[str, list[str]] = defaultdict(list)
    for raw_code, raw_digest in rows:
        if not isinstance(raw_code, str) or not isinstance(raw_digest, str):
            continue
        code = raw_code.strip()
        digest = raw_digest.strip()
        if code and digest and digest not in hashes[code]:
            hashes[code].append(digest)
    return {code: tuple(digests) for code, digests in hashes.items()}


def _default_photo_opener() -> PhotoOpener:
    from binkeeper.blob_vault import (
        BlobVaultError,
        blob_store_from_config,
        open_blob,
        vault_key_from_config,
    )

    store = blob_store_from_config()
    key, _key_ref = vault_key_from_config()

    def open_owner_photo(conn: psycopg.Connection, sha256: str) -> bytes | None:
        try:
            return open_blob(conn, store, sha256, key=key)
        except BlobVaultError:
            return None

    return open_owner_photo


def _normalize(text: str) -> str:
    return " ".join(match.group(0).casefold() for match in _TOKEN_RE.finditer(text))


def _evidence_key(event: LabelDriftEvidence) -> tuple[datetime, str]:
    return event.observed_at, event.external_id


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _mappings(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(mapping for item in value if (mapping := _mapping(item)) is not None)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str) and item.strip())


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _required_text(value: object, field_name: str) -> str:
    text = _text(value)
    if text is None:
        raise LabelDriftError(f"{field_name} is required")
    return text


def _action_id(value: str) -> str:
    try:
        return str(UUID(value.strip()))
    except (AttributeError, ValueError) as exc:
        raise LabelDriftError("action_id must be a UUID") from exc


def _aware_datetime(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LabelDriftError(f"{field_name} must include a timezone")
    return value.astimezone(UTC)
