"""RFC 0093 P0 — read-only bin passport projector.

A bin passport is a rebuildable profile for one physical bin. This module keeps
the first slice deliberately small: pure projection from bin-capture metadata plus
optional move/belief folds, with read-only loaders over the existing tables. It
does not write rows, run models, call vision, or promote JSONB into new schema.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from typing import Final, Literal, cast

import psycopg

from binkeeper.bin_inventory import (
    DEFAULT_BIN_CORPUS_ID,
    DEFAULT_BIN_TENANT_ID,
    BinBelief,
    BinLocation,
    bin_belief,
)

BIN_CAPTURE_KIND: Final[str] = "bin_capture"
BIN_PASSPORT_PROJECTOR_VERSION: Final[str] = os.environ.get(
    "BINKEEPER_BIN_PASSPORT_PROJECTOR_VERSION", "bin-passport.v1.rfc0093-p0"
)

CapacityState = Literal["unknown", "empty", "sparse", "half", "tight", "full", "overfull"]
VolumeUnit = Literal["l", "ml", "gal", "qt", "cu_ft", "unknown"]

CAPACITY_STATES: Final[tuple[CapacityState, ...]] = (
    "unknown",
    "empty",
    "sparse",
    "half",
    "tight",
    "full",
    "overfull",
)
VOLUME_UNITS: Final[tuple[VolumeUnit, ...]] = ("l", "ml", "gal", "qt", "cu_ft", "unknown")
VOLUME_UNIT_FACTORS_L: Final[dict[VolumeUnit, float | None]] = {
    "l": 1.0,
    "ml": 0.001,
    "gal": 3.785411784,
    "qt": 0.946352946,
    "cu_ft": 28.316846592,
    "unknown": None,
}
_PROFILE_KEYS: Final[tuple[str, ...]] = (
    "theme",
    "home_site",
    "owner_phrase",
    "accepts",
    "excludes",
    "examples",
    "physical_constraints",
    "volume_label",
    "volume_value",
    "volume_unit",
    "canonical_volume_liters",
    "external_dimensions_cm",
    "internal_dimensions_cm",
    "form_factor",
    "mouth_constraint",
    "stacking_constraint",
    "max_weight_kg",
    "capacity_state",
    "confidence",
)
_DEFAULT_PROFILE_SNAPSHOT_FIELDS: Final[frozenset[str]] = frozenset(
    {"theme", "home_site", "contents_text"}
)
_VOLUME_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>cubic\s+feet|cubic\s+foot|cu\s*ft|cu_ft|ft3|"
    r"gallons?|gal|quarts?|qt|liters?|litres?|l|milliliters?|millilitres?|ml)\b",
    re.IGNORECASE,
)


class BinPassportError(ValueError):
    """Domain root for bin-passport projector failures (RFC 0012 family)."""


@dataclass(frozen=True)
class DimensionsCm:
    """Container dimensions in centimetres."""

    length: float | None = None
    width: float | None = None
    height: float | None = None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for dimensions."""
        return {
            "length": self.length,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class BinVolumeProfile:
    """Nominal volume and physical container metadata, separate from capacity."""

    volume_label: str | None = None
    volume_value: float | None = None
    volume_unit: VolumeUnit = "unknown"
    canonical_volume_liters: float | None = None
    external_dimensions_cm: DimensionsCm | None = None
    internal_dimensions_cm: DimensionsCm | None = None
    form_factor: str | None = None
    mouth_constraint: str | None = None
    stacking_constraint: str | None = None
    max_weight_kg: float | None = None
    source_ref: str | None = None
    confidence: float | None = None
    as_of: datetime | None = None

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a volume profile."""
        return {
            "volume_label": self.volume_label,
            "volume_value": self.volume_value,
            "volume_unit": self.volume_unit,
            "canonical_volume_liters": (
                round(self.canonical_volume_liters, 4)
                if self.canonical_volume_liters is not None
                else None
            ),
            "external_dimensions_cm": (
                self.external_dimensions_cm.to_json() if self.external_dimensions_cm else None
            ),
            "internal_dimensions_cm": (
                self.internal_dimensions_cm.to_json() if self.internal_dimensions_cm else None
            ),
            "form_factor": self.form_factor,
            "mouth_constraint": self.mouth_constraint,
            "stacking_constraint": self.stacking_constraint,
            "max_weight_kg": self.max_weight_kg,
            "source_ref": self.source_ref,
            "confidence": self.confidence,
            "as_of": self.as_of.isoformat() if self.as_of else None,
        }


@dataclass(frozen=True)
class BinCaptureEvidence:
    """One append-only bin_capture row as the passport projector sees it."""

    external_id: str
    bin_code: str
    site: str | None = None
    contents_text: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    captured_at: datetime | None = None
    observed_at: datetime | None = None
    imported_at: datetime | None = None
    content_text: str | None = None

    @property
    def effective_at(self) -> datetime | None:
        """The best available time for ordering this evidence."""
        return self.captured_at or self.observed_at or self.imported_at

    def provenance_ref(self, path: str) -> str:
        """Return a stable provenance pointer into this capture's metadata."""
        return f"capture:{self.external_id}#metadata.{path}"


@dataclass(frozen=True)
class BinPassport:
    """Rebuildable placement profile for one bin."""

    bin_code: str
    theme: str | None
    home_site: str | None
    current_site: str | None
    owner_phrase: str | None
    accepts: tuple[str, ...]
    excludes: tuple[str, ...]
    examples: tuple[str, ...]
    sibling_contents: tuple[str, ...]
    physical_constraints: tuple[str, ...]
    volume_profile: BinVolumeProfile | None
    capacity_state: CapacityState
    location_confidence: float
    passport_confidence: float
    provenance_refs: tuple[str, ...]
    projector_version: str = BIN_PASSPORT_PROJECTOR_VERSION

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for a passport."""
        return {
            "bin_code": self.bin_code,
            "theme": self.theme,
            "home_site": self.home_site,
            "current_site": self.current_site,
            "owner_phrase": self.owner_phrase,
            "accepts": list(self.accepts),
            "excludes": list(self.excludes),
            "examples": list(self.examples),
            "sibling_contents": list(self.sibling_contents),
            "physical_constraints": list(self.physical_constraints),
            "volume_profile": self.volume_profile.to_json() if self.volume_profile else None,
            "capacity_state": self.capacity_state,
            "location_confidence": round(self.location_confidence, 4),
            "passport_confidence": round(self.passport_confidence, 4),
            "provenance_refs": list(self.provenance_refs),
            "projector_version": self.projector_version,
        }


def canonical_volume_liters(value: float, unit: VolumeUnit) -> float | None:
    """Normalize a volume value to litres when the unit is known."""
    factor = VOLUME_UNIT_FACTORS_L.get(unit)
    if factor is None:
        return None
    return value * factor


def parse_volume_label(label: str) -> dict[str, object] | None:
    """Parse a conservative owner volume label such as ``27 gal tote``.

    If a future ``binkeeper.bin_volume.parse_volume_label`` exists, this function
    accepts its mapping/dataclass result. The local fallback handles only common
    explicit volume labels and refuses ambiguous names such as ``shoebox``.
    """
    external = _parse_with_external_volume_parser(label)
    if external is not None:
        return external
    match = _VOLUME_LABEL_PATTERN.search(label)
    if match is None:
        return None
    value = _float_value(match.group("value"))
    unit = _normalize_volume_unit(match.group("unit"))
    if value is None or unit == "unknown":
        return None
    return {
        "volume_value": value,
        "volume_unit": unit,
        "canonical_volume_liters": canonical_volume_liters(value, unit),
    }


def build_volume_profile(
    profile: Mapping[str, object],
    *,
    source_ref: str | None = None,
    as_of: datetime | None = None,
) -> BinVolumeProfile | None:
    """Project volume metadata from one owner/capture profile. Pure and total."""
    volume_label = _string_value(profile.get("volume_label"))
    volume_value = _float_value(profile.get("volume_value"))
    volume_unit = _normalize_volume_unit(_string_value(profile.get("volume_unit")))
    canonical = _float_value(profile.get("canonical_volume_liters"))

    if canonical is None and volume_value is not None:
        canonical = canonical_volume_liters(volume_value, volume_unit)
    if volume_label and (volume_value is None or volume_unit == "unknown" or canonical is None):
        parsed = parse_volume_label(volume_label)
        if parsed is not None:
            volume_value = (
                volume_value
                if volume_value is not None
                else _float_value(parsed.get("volume_value"))
            )
            if volume_unit == "unknown":
                volume_unit = _normalize_volume_unit(_string_value(parsed.get("volume_unit")))
            canonical = (
                canonical
                if canonical is not None
                else _float_value(parsed.get("canonical_volume_liters"))
            )

    external_dimensions = _dimensions(profile.get("external_dimensions_cm"))
    internal_dimensions = _dimensions(profile.get("internal_dimensions_cm"))
    form_factor = _string_value(profile.get("form_factor"))
    mouth_constraint = _string_value(profile.get("mouth_constraint"))
    stacking_constraint = _string_value(profile.get("stacking_constraint"))
    max_weight = _float_value(profile.get("max_weight_kg"))
    confidence = _clamp01(_float_value(profile.get("confidence")))

    if not any(
        (
            volume_label,
            volume_value is not None,
            canonical is not None,
            external_dimensions,
            internal_dimensions,
            form_factor,
            mouth_constraint,
            stacking_constraint,
            max_weight is not None,
        )
    ):
        return None

    return BinVolumeProfile(
        volume_label=volume_label,
        volume_value=volume_value,
        volume_unit=volume_unit,
        canonical_volume_liters=canonical,
        external_dimensions_cm=external_dimensions,
        internal_dimensions_cm=internal_dimensions,
        form_factor=form_factor,
        mouth_constraint=mouth_constraint,
        stacking_constraint=stacking_constraint,
        max_weight_kg=max_weight,
        source_ref=source_ref,
        confidence=confidence,
        as_of=as_of,
    )


def capture_from_payload(
    *,
    external_id: str,
    raw_payload: object,
    observed_at: datetime | None = None,
    imported_at: datetime | None = None,
    content_text: str | None = None,
) -> BinCaptureEvidence | None:
    """Map one captures row payload to bin-capture evidence, or None if out of scope."""
    payload = _mapping(raw_payload)
    if payload is None:
        return None
    metadata = _mapping(payload.get("metadata"))
    if metadata is None or metadata.get("kind") != BIN_CAPTURE_KIND:
        return None
    bin_code = _string_value(metadata.get("bin_code"))
    if bin_code is None:
        return None
    return BinCaptureEvidence(
        external_id=external_id,
        bin_code=bin_code,
        site=_string_value(metadata.get("site")),
        contents_text=_string_value(metadata.get("contents_text")),
        metadata=metadata,
        captured_at=_datetime_value(metadata.get("captured_at")),
        observed_at=observed_at,
        imported_at=imported_at,
        content_text=content_text,
    )


def project_bin_passport(
    bin_code: str,
    captures: Sequence[BinCaptureEvidence],
    *,
    location: BinLocation | None = None,
    belief: BinBelief | None = None,
    projector_version: str = BIN_PASSPORT_PROJECTOR_VERSION,
) -> BinPassport:
    """Build one read-only passport from captures and optional location belief."""
    own_captures = _ordered_captures(
        capture for capture in captures if capture.bin_code == bin_code
    )
    resolved_location = belief.location if belief is not None else location
    latest_site_capture = next(
        (capture for capture in reversed(own_captures) if capture.site is not None),
        None,
    )

    theme = _latest_profile_string(own_captures, "theme")
    owner_phrase = _latest_profile_string(own_captures, "owner_phrase")
    home_site = _latest_profile_string(own_captures, "home_site")
    if home_site is None and not any(
        _is_profile_snapshot_for(capture, "home_site") for capture in own_captures
    ):
        home_site = latest_site_capture.site if latest_site_capture else None
    accepts = _aggregate_profile_strings(own_captures, "accepts")
    excludes = _aggregate_profile_strings(own_captures, "excludes")
    examples = _aggregate_profile_strings(own_captures, "examples")
    physical_constraints = _aggregate_profile_strings(own_captures, "physical_constraints")
    sibling_contents = _sibling_contents(own_captures)
    capacity_state = _capacity_state(_latest_profile_value(own_captures, "capacity_state"))
    volume_profile = _latest_volume_profile(own_captures)

    current_site, location_confidence, location_ref = _current_site(
        resolved_location, belief, latest_site_capture
    )
    provenance_refs = _provenance_refs(
        bin_code=bin_code,
        captures=own_captures,
        current_site_ref=location_ref,
        volume_profile=volume_profile,
    )
    passport_confidence = _passport_confidence(
        capture_count=len(own_captures),
        sibling_contents=sibling_contents,
        theme=theme,
        owner_phrase=owner_phrase,
        accepts=accepts,
        examples=examples,
        volume_profile=volume_profile,
        location_confidence=location_confidence,
    )
    return BinPassport(
        bin_code=bin_code,
        theme=theme,
        home_site=home_site,
        current_site=current_site,
        owner_phrase=owner_phrase,
        accepts=accepts,
        excludes=excludes,
        examples=examples,
        sibling_contents=sibling_contents,
        physical_constraints=physical_constraints,
        volume_profile=volume_profile,
        capacity_state=capacity_state,
        location_confidence=location_confidence,
        passport_confidence=passport_confidence,
        provenance_refs=provenance_refs,
        projector_version=projector_version,
    )


def load_bin_captures(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[BinCaptureEvidence]:
    """Load append-only bin_capture evidence for one bin. Read-only."""
    rows = conn.execute(
        """
        SELECT external_id, raw_payload, observed_at, imported_at, content_text
        FROM captures
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND raw_payload->'metadata'->>'kind' = %s
          AND raw_payload->'metadata'->>'bin_code' = %s
        ORDER BY COALESCE(observed_at, imported_at), imported_at, external_id
        """,
        (tenant_id, corpus_id, BIN_CAPTURE_KIND, bin_code),
    ).fetchall()
    captures: list[BinCaptureEvidence] = []
    for external_id, raw_payload, observed_at, imported_at, content_text in rows:
        capture = capture_from_payload(
            external_id=str(external_id),
            raw_payload=raw_payload,
            observed_at=observed_at,  # type: ignore[arg-type]
            imported_at=imported_at,  # type: ignore[arg-type]
            content_text=content_text,  # type: ignore[arg-type]
        )
        if capture is not None:
            captures.append(capture)
    return captures


def load_known_bin_codes(
    conn: psycopg.Connection,
    *,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[str]:
    """Return bins known through captures or the move ledger. Read-only."""
    rows = conn.execute(
        """
        SELECT DISTINCT bin_code FROM (
            SELECT raw_payload->'metadata'->>'bin_code' AS bin_code
            FROM captures
            WHERE tenant_id = %s
              AND corpus_id = %s
              AND raw_payload->'metadata'->>'kind' = %s
            UNION
            SELECT bin_code
            FROM bin_trip_events
            WHERE tenant_id = %s
              AND corpus_id = %s
              AND bin_code IS NOT NULL
        ) known
        WHERE bin_code IS NOT NULL AND btrim(bin_code) <> ''
        ORDER BY bin_code
        """,
        (tenant_id, corpus_id, BIN_CAPTURE_KIND, tenant_id, corpus_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


def bin_passport(
    conn: psycopg.Connection,
    bin_code: str,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> BinPassport:
    """Load existing evidence and project one bin passport. Read-only."""
    captures = load_bin_captures(conn, bin_code, tenant_id=tenant_id, corpus_id=corpus_id)
    belief = bin_belief(conn, bin_code, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
    return project_bin_passport(bin_code, captures, belief=belief)


def load_bin_passports(
    conn: psycopg.Connection,
    *,
    now: datetime | None = None,
    tenant_id: str = DEFAULT_BIN_TENANT_ID,
    corpus_id: str = DEFAULT_BIN_CORPUS_ID,
) -> list[BinPassport]:
    """Project passports for all bins known to existing evidence. Read-only."""
    return [
        bin_passport(conn, code, now=now, tenant_id=tenant_id, corpus_id=corpus_id)
        for code in load_known_bin_codes(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    ]


def _parse_with_external_volume_parser(label: str) -> dict[str, object] | None:
    """Use the shared binkeeper.bin_volume parser when one is installed."""
    try:
        module = import_module("binkeeper.bin_volume")
    except ModuleNotFoundError as exc:
        if exc.name != "binkeeper.bin_volume":
            raise
        return None
    parser_object = getattr(module, "parse_volume_label", None) or getattr(
        module, "parse_volume_profile", None
    )
    if not callable(parser_object):
        return None
    parser = cast(Callable[[str], object], parser_object)
    try:
        parsed = parser(label)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if isinstance(parsed, Mapping):
        result = dict(parsed)
        if _parsed_volume_is_empty(result):
            return None
        return result
    result: dict[str, object] = {}
    for attr in ("volume_value", "volume_unit", "canonical_volume_liters"):
        if hasattr(parsed, attr):
            result[attr] = getattr(parsed, attr)
    if _parsed_volume_is_empty(result):
        return None
    return result or None


def _parsed_volume_is_empty(parsed: Mapping[str, object]) -> bool:
    """True when a shared parser preserved a label but found no nominal volume."""
    value = _float_value(parsed.get("volume_value"))
    unit = _normalize_volume_unit(_string_value(parsed.get("volume_unit")))
    canonical = _float_value(parsed.get("canonical_volume_liters"))
    return value is None and unit == "unknown" and canonical is None


def _normalize_volume_unit(value: str | None) -> VolumeUnit:
    """Normalize owner/API volume-unit spellings to the RFC 0093 unit vocabulary."""
    if value is None:
        return "unknown"
    normalized = value.strip().lower().replace(".", "").replace("_", " ")
    if normalized in {"l", "liter", "liters", "litre", "litres"}:
        return "l"
    if normalized in {"ml", "milliliter", "milliliters", "millilitre", "millilitres"}:
        return "ml"
    if normalized in {"gal", "gallon", "gallons"}:
        return "gal"
    if normalized in {"qt", "quart", "quarts"}:
        return "qt"
    if normalized in {"cu ft", "cubic foot", "cubic feet", "ft3"}:
        return "cu_ft"
    if normalized in VOLUME_UNITS:
        return cast(VolumeUnit, normalized)
    return "unknown"


def _mapping(value: object) -> Mapping[str, object] | None:
    """Return value as a string-keyed mapping when possible."""
    if not isinstance(value, Mapping):
        return None
    return {str(key): item for key, item in value.items()}


def _string_value(value: object) -> str | None:
    """Return a stripped non-empty string, or None."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _float_value(value: object) -> float | None:
    """Return a finite-ish float for JSON scalar values."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _datetime_value(value: object) -> datetime | None:
    """Parse an ISO datetime from JSON metadata."""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _dimensions(value: object) -> DimensionsCm | None:
    """Parse optional dimension metadata from a JSON object."""
    raw = _mapping(value)
    if raw is None:
        return None
    dimensions = DimensionsCm(
        length=_float_value(raw.get("length")),
        width=_float_value(raw.get("width")),
        height=_float_value(raw.get("height")),
    )
    if dimensions.length is None and dimensions.width is None and dimensions.height is None:
        return None
    return dimensions


def _clamp01(value: float | None) -> float | None:
    """Clamp a value to [0, 1], preserving None."""
    if value is None:
        return None
    return max(0.0, min(1.0, value))


def _aware(value: datetime | None) -> datetime:
    """Return a timezone-aware sort key for optional datetimes."""
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _ordered_captures(captures: Iterable[BinCaptureEvidence]) -> list[BinCaptureEvidence]:
    """Order captures by observation time, then external id."""
    return sorted(
        captures,
        key=lambda capture: (_aware(capture.effective_at), capture.external_id),
    )


def _profile_for(capture: BinCaptureEvidence) -> dict[str, object]:
    """Merge top-level metadata profile keys with metadata.bin_profile."""
    profile: dict[str, object] = {
        key: capture.metadata[key] for key in _PROFILE_KEYS if key in capture.metadata
    }
    nested = _mapping(capture.metadata.get("bin_profile"))
    if nested is not None:
        profile.update(nested)
    return profile


def _is_profile_snapshot(capture: BinCaptureEvidence) -> bool:
    """Return whether this capture replaces the bin's current owner profile."""
    return capture.metadata.get("profile_mode") == "snapshot"


def _is_profile_snapshot_for(capture: BinCaptureEvidence, key: str) -> bool:
    """Return whether this snapshot explicitly owns the named current field."""
    if not _is_profile_snapshot(capture):
        return False
    declared = capture.metadata.get("profile_fields")
    if isinstance(declared, Sequence) and not isinstance(declared, str | bytes | bytearray):
        return key in declared
    return key in _DEFAULT_PROFILE_SNAPSHOT_FIELDS


def _latest_profile_string(captures: Sequence[BinCaptureEvidence], key: str) -> str | None:
    """Return the latest string value, respecting an explicit snapshot clear."""
    for capture in reversed(captures):
        value = _string_value(_profile_for(capture).get(key))
        if value is not None:
            return value
        if _is_profile_snapshot_for(capture, key):
            return None
    return None


def _latest_profile_value(captures: Sequence[BinCaptureEvidence], key: str) -> object | None:
    """Return the latest raw profile value, respecting key-scoped snapshots."""
    for capture in reversed(captures):
        profile = _profile_for(capture)
        if key in profile:
            return profile[key]
        if _is_profile_snapshot_for(capture, key):
            return None
    return None


def _string_tuple(value: object) -> tuple[str, ...]:
    """Normalize a string or JSON string list to a tuple."""
    single = _string_value(value)
    if single is not None:
        return (single,)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        items: list[str] = []
        for item in value:
            text = _string_value(item)
            if text is not None:
                items.append(text)
        return tuple(items)
    return ()


def _aggregate_profile_strings(captures: Sequence[BinCaptureEvidence], key: str) -> tuple[str, ...]:
    """Aggregate first-seen profile string values for list-like fields."""
    values: list[str] = []
    for capture in captures:
        for item in _string_tuple(_profile_for(capture).get(key)):
            if item not in values:
                values.append(item)
    return tuple(values)


def _sibling_contents(captures: Sequence[BinCaptureEvidence]) -> tuple[str, ...]:
    """Project current content notes, resetting at the latest full snapshot."""
    current_captures = captures
    for index in range(len(captures) - 1, -1, -1):
        if _is_profile_snapshot_for(captures[index], "contents_text"):
            current_captures = captures[index:]
            break
    values: list[str] = []
    for capture in current_captures:
        content = (
            capture.contents_text if "contents_text" in capture.metadata else capture.content_text
        )
        if content and content not in values:
            values.append(content)
    return tuple(values)


def _capacity_state(value: object) -> CapacityState:
    """Normalize current capacity state, keeping invalid values as unknown."""
    text = _string_value(value)
    if text in CAPACITY_STATES:
        return cast(CapacityState, text)
    return "unknown"


def _latest_volume_profile(captures: Sequence[BinCaptureEvidence]) -> BinVolumeProfile | None:
    """Return the latest capture's volume profile, if any."""
    for capture in reversed(captures):
        profile = _profile_for(capture)
        source_ref = (
            capture.provenance_ref("bin_profile")
            if isinstance(capture.metadata.get("bin_profile"), Mapping)
            else capture.provenance_ref("volume")
        )
        volume = build_volume_profile(profile, source_ref=source_ref, as_of=capture.effective_at)
        if volume is not None:
            return volume
    return None


def _current_site(
    location: BinLocation | None,
    belief: BinBelief | None,
    latest_capture: BinCaptureEvidence | None,
) -> tuple[str | None, float, str | None]:
    """Resolve current site and confidence without mutating evidence."""
    if location is not None and location.status == "at_site" and location.site is not None:
        ref = (
            f"bin_trip_events:seq:{location.last_event_seq}"
            if location.last_event_seq is not None
            else None
        )
        confidence = belief.confidence if belief is not None else 1.0
        return location.site, _clamp01(confidence) or 0.0, ref
    if location is not None and location.status == "in_transit":
        ref = (
            f"bin_trip_events:seq:{location.last_event_seq}"
            if location.last_event_seq is not None
            else None
        )
        confidence = belief.confidence if belief is not None else 0.0
        return None, _clamp01(confidence) or 0.0, ref
    if latest_capture is not None and latest_capture.site is not None:
        return latest_capture.site, 0.65, latest_capture.provenance_ref("site")
    return None, 0.0, None


def _passport_confidence(
    *,
    capture_count: int,
    sibling_contents: Sequence[str],
    theme: str | None,
    owner_phrase: str | None,
    accepts: Sequence[str],
    examples: Sequence[str],
    volume_profile: BinVolumeProfile | None,
    location_confidence: float,
) -> float:
    """Small bounded confidence heuristic for the profile completeness."""
    confidence = 0.0
    if capture_count:
        confidence += 0.3
    if sibling_contents:
        confidence += 0.15
    if theme or owner_phrase:
        confidence += 0.15
    if accepts or examples:
        confidence += 0.15
    if volume_profile is not None:
        confidence += 0.1
    confidence += min(0.15, 0.15 * max(0.0, min(1.0, location_confidence)))
    return max(0.0, min(1.0, confidence))


def _profile_field_ref(capture: BinCaptureEvidence, key: str) -> str | None:
    """Return the metadata path that supplied a profile key."""
    nested = _mapping(capture.metadata.get("bin_profile"))
    if nested is not None and key in nested:
        return capture.provenance_ref(f"bin_profile.{key}")
    if key in capture.metadata:
        return capture.provenance_ref(key)
    return None


def _provenance_refs(
    *,
    bin_code: str,
    captures: Sequence[BinCaptureEvidence],
    current_site_ref: str | None,
    volume_profile: BinVolumeProfile | None,
) -> tuple[str, ...]:
    """Build stable refs to the evidence fields used by the passport."""
    refs: list[str] = []
    for capture in captures:
        if capture.bin_code == bin_code:
            refs.append(capture.provenance_ref("bin_code"))
        if capture.site is not None:
            refs.append(capture.provenance_ref("site"))
        if capture.contents_text is not None:
            refs.append(capture.provenance_ref("contents_text"))
        for key in _PROFILE_KEYS:
            ref = _profile_field_ref(capture, key)
            if ref is not None:
                refs.append(ref)
    if current_site_ref is not None:
        refs.append(current_site_ref)
    if volume_profile is not None and volume_profile.source_ref is not None:
        refs.append(volume_profile.source_ref)
    return _unique(refs)


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    """Return values in first-seen order without duplicates."""
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return tuple(seen)
