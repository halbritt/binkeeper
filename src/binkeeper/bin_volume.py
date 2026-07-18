"""RFC 0093 P0 — pure bin volume metadata parsing.

The placement router needs nominal container size as stable profile metadata,
separate from dynamic capacity. This module keeps the first slice deliberately
small and database-free: preserve the owner's raw volume label, normalize common
storage-bin units to liters, infer a coarse form factor, and normalize rough
capacity state.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

VolumeUnit = Literal["l", "ml", "gal", "qt", "cu_ft", "unknown"]
FormFactor = Literal[
    "lidded_tote",
    "open_tote",
    "drawer",
    "shelf_bin",
    "bucket",
    "bag",
    "box",
    "tube",
    "envelope",
    "crate",
    "unknown",
]
CapacityState = Literal["unknown", "empty", "sparse", "half", "tight", "full", "overfull"]

VOLUME_UNITS: Final[tuple[VolumeUnit, ...]] = ("l", "ml", "gal", "qt", "cu_ft", "unknown")
FORM_FACTORS: Final[tuple[FormFactor, ...]] = (
    "lidded_tote",
    "open_tote",
    "drawer",
    "shelf_bin",
    "bucket",
    "bag",
    "box",
    "tube",
    "envelope",
    "crate",
    "unknown",
)
CAPACITY_STATES: Final[tuple[CapacityState, ...]] = (
    "unknown",
    "empty",
    "sparse",
    "half",
    "tight",
    "full",
    "overfull",
)

LITERS_PER_UNIT: Final[dict[VolumeUnit, float | None]] = {
    "l": 1.0,
    "ml": 0.001,
    "gal": 3.785411784,
    "qt": 0.946352946,
    "cu_ft": 28.316846592,
    "unknown": None,
}

_NUMBER_PATTERN: Final[str] = r"(?:\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)"
_UNIT_PATTERN: Final[str] = (
    r"(?:cu\.?\s*ft\.?|cubic\s*(?:feet|foot|ft)|ft\^?3|cuft|cft|"
    r"gallons?|gals?|gal|quarts?|qts?|qt|lit(?:er|re)s?|l|"
    r"millilit(?:er|re)s?|ml)"
)
_VOLUME_RE: Final[re.Pattern[str]] = re.compile(
    rf"(?<![\w/])(?P<value>{_NUMBER_PATTERN})\s*-?\s*(?P<unit>{_UNIT_PATTERN})\b",
    re.IGNORECASE,
)

_UNIT_ALIASES: Final[dict[str, VolumeUnit]] = {
    "l": "l",
    "liter": "l",
    "liters": "l",
    "litre": "l",
    "litres": "l",
    "ml": "ml",
    "milliliter": "ml",
    "milliliters": "ml",
    "millilitre": "ml",
    "millilitres": "ml",
    "gal": "gal",
    "gals": "gal",
    "gallon": "gal",
    "gallons": "gal",
    "qt": "qt",
    "qts": "qt",
    "quart": "qt",
    "quarts": "qt",
    "cu_ft": "cu_ft",
    "cuft": "cu_ft",
    "cft": "cu_ft",
    "cubic_ft": "cu_ft",
    "cubic_foot": "cu_ft",
    "cubic_feet": "cu_ft",
    "ft3": "cu_ft",
    "ft_3": "cu_ft",
    "unknown": "unknown",
}

_FORM_FACTOR_ALIASES: Final[dict[str, FormFactor]] = {
    "lidded_tote": "lidded_tote",
    "tote": "lidded_tote",
    "storage_tote": "lidded_tote",
    "rubbermaid": "lidded_tote",
    "open_tote": "open_tote",
    "drawer": "drawer",
    "shelf_bin": "shelf_bin",
    "parts_bin": "shelf_bin",
    "bucket": "bucket",
    "pail": "bucket",
    "bag": "bag",
    "sack": "bag",
    "box": "box",
    "shoebox": "box",
    "shoe_box": "box",
    "tube": "tube",
    "envelope": "envelope",
    "crate": "crate",
    "unknown": "unknown",
}

_FORM_FACTOR_PATTERNS: Final[tuple[tuple[re.Pattern[str], FormFactor], ...]] = (
    (re.compile(r"\bopen[-\s]+(?:top[-\s]+)?tote\b", re.IGNORECASE), "open_tote"),
    (re.compile(r"\b(?:lidded|lid|storage|rubbermaid)[-\s]+tote\b", re.IGNORECASE), "lidded_tote"),
    (re.compile(r"\btote\b", re.IGNORECASE), "lidded_tote"),
    (re.compile(r"\b(?:shelf|parts)[-\s]+bin\b", re.IGNORECASE), "shelf_bin"),
    (re.compile(r"\bdrawer\b", re.IGNORECASE), "drawer"),
    (re.compile(r"\b(?:bucket|pail)\b", re.IGNORECASE), "bucket"),
    (re.compile(r"\benvelope\b", re.IGNORECASE), "envelope"),
    (re.compile(r"\btube\b", re.IGNORECASE), "tube"),
    (re.compile(r"\bcrate\b", re.IGNORECASE), "crate"),
    (re.compile(r"\b(?:bag|sack)\b", re.IGNORECASE), "bag"),
    (re.compile(r"\b(?:shoe[-\s]?box|box)\b", re.IGNORECASE), "box"),
)

_CAPACITY_ALIASES: Final[dict[str, CapacityState]] = {
    "unknown": "unknown",
    "empty": "empty",
    "sparse": "sparse",
    "light": "sparse",
    "low": "sparse",
    "half": "half",
    "half_full": "half",
    "half_empty": "half",
    "tight": "tight",
    "packed": "tight",
    "crowded": "tight",
    "mostly_full": "tight",
    "nearly_full": "tight",
    "almost_full": "tight",
    "full": "full",
    "overfull": "overfull",
    "over_full": "overfull",
    "overflowing": "overfull",
}
_CAPACITY_PATTERNS: Final[tuple[tuple[re.Pattern[str], CapacityState], ...]] = (
    (
        re.compile(r"\b(?:over\s*full|overfull|overflowing|overflowed|too\s+full)\b", re.I),
        "overfull",
    ),
    (
        re.compile(
            r"\b(?:tight|packed|crowded|jammed|mostly\s+full|nearly\s+full|almost\s+full)\b",
            re.I,
        ),
        "tight",
    ),
    (re.compile(r"\bhalf[-/\s]+full\b", re.I), "half"),
    (re.compile(r"\bfull\b", re.I), "full"),
    (re.compile(r"\bhalf\b", re.I), "half"),
    (re.compile(r"\b(?:sparse|light|low|mostly\s+empty)\b", re.I), "sparse"),
    (re.compile(r"\bempty\b", re.I), "empty"),
    (re.compile(r"\bunknown\b", re.I), "unknown"),
)


@dataclass(frozen=True)
class VolumeProfile:
    """Normalized volume metadata for a bin passport."""

    volume_label: str | None
    volume_value: float | None
    volume_unit: VolumeUnit
    canonical_volume_liters: float | None
    form_factor: FormFactor
    capacity_state: CapacityState

    def to_json(self) -> dict[str, object]:
        """Return the stable JSON shape for volume profile metadata."""
        return {
            "volume_label": self.volume_label,
            "volume_value": self.volume_value,
            "volume_unit": self.volume_unit,
            "canonical_volume_liters": self.canonical_volume_liters,
            "form_factor": self.form_factor,
            "capacity_state": self.capacity_state,
        }


def normalize_volume_metadata(metadata: Mapping[str, object] | None) -> VolumeProfile:
    """Normalize RFC 0093 volume fields from flat metadata or nested ``bin_profile``."""
    if metadata is None:
        return parse_volume_profile()
    source: Mapping[str, object] = metadata
    profile = metadata.get("bin_profile")
    if isinstance(profile, Mapping):
        source = {str(key): value for key, value in profile.items()}
    return parse_volume_profile(
        source.get("volume_label"),
        volume_value=source.get("volume_value"),
        volume_unit=source.get("volume_unit"),
        form_factor=source.get("form_factor"),
        capacity_state=source.get("capacity_state"),
    )


def parse_volume_profile(
    volume_label: object | None = None,
    *,
    volume_value: object | None = None,
    volume_unit: object | None = None,
    form_factor: object | None = None,
    capacity_state: object | None = None,
) -> VolumeProfile:
    """Parse and normalize one bin's nominal volume metadata."""
    label = _text_or_none(volume_label)
    label_value, label_unit = _parse_labeled_volume(label)
    explicit_value = _coerce_positive_float(volume_value)
    explicit_unit = _normalize_volume_unit(volume_unit)

    if explicit_value is not None:
        normalized_value = explicit_value
        normalized_unit = explicit_unit or label_unit or "unknown"
    elif label_value is not None:
        normalized_value = label_value
        normalized_unit = explicit_unit or label_unit or "unknown"
    else:
        normalized_value = None
        normalized_unit = explicit_unit or "unknown"

    return VolumeProfile(
        volume_label=label,
        volume_value=normalized_value,
        volume_unit=normalized_unit,
        canonical_volume_liters=_canonical_liters(normalized_value, normalized_unit),
        form_factor=_normalize_form_factor(form_factor) or _infer_form_factor(label),
        capacity_state=_normalize_capacity_state(capacity_state) or _infer_capacity_state(label),
    )


def _text_or_none(value: object | None) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _parse_labeled_volume(label: str | None) -> tuple[float | None, VolumeUnit | None]:
    if label is None:
        return None, None
    match = _VOLUME_RE.search(label)
    if match is None:
        return None, None
    value = _parse_number(match.group("value"))
    unit = _normalize_volume_unit(match.group("unit"))
    if value is None or unit is None:
        return None, None
    return value, unit


def _parse_number(text: str) -> float | None:
    stripped = text.strip()
    if " " in stripped and "/" in stripped:
        whole, fraction = stripped.split(maxsplit=1)
        whole_value = _parse_number(whole)
        fraction_value = _parse_number(fraction)
        if whole_value is None or fraction_value is None:
            return None
        return whole_value + fraction_value
    if "/" in stripped:
        numerator_text, _, denominator_text = stripped.partition("/")
        try:
            numerator = float(numerator_text)
            denominator = float(denominator_text)
        except ValueError:
            return None
        if denominator == 0:
            return None
        value = numerator / denominator
        return value if value > 0 else None
    try:
        value = float(stripped)
    except ValueError:
        return None
    return value if value > 0 else None


def _coerce_positive_float(value: object | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number > 0 else None
    text = _text_or_none(value)
    if text is None:
        return None
    return _parse_number(text)


def _normalize_volume_unit(value: object | None) -> VolumeUnit | None:
    text = _text_or_none(value)
    if text is None:
        return None
    key = _slug(text.replace("^", ""))
    if key == "ft_3":
        key = "ft3"
    return _UNIT_ALIASES.get(key)


def _canonical_liters(value: float | None, unit: VolumeUnit) -> float | None:
    if value is None:
        return None
    multiplier = LITERS_PER_UNIT[unit]
    if multiplier is None:
        return None
    return value * multiplier


def _normalize_form_factor(value: object | None) -> FormFactor | None:
    text = _text_or_none(value)
    if text is None:
        return None
    return _FORM_FACTOR_ALIASES.get(_slug(text))


def _infer_form_factor(label: str | None) -> FormFactor:
    if label is None:
        return "unknown"
    for pattern, form_factor in _FORM_FACTOR_PATTERNS:
        if pattern.search(label):
            return form_factor
    return "unknown"


def _normalize_capacity_state(value: object | None) -> CapacityState | None:
    text = _text_or_none(value)
    if text is None:
        return None
    exact = _CAPACITY_ALIASES.get(_slug(text))
    if exact is not None:
        return exact
    for pattern, state in _CAPACITY_PATTERNS:
        if pattern.search(text):
            return state
    return None


def _infer_capacity_state(label: str | None) -> CapacityState:
    return _normalize_capacity_state(label) or "unknown"
