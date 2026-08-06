"""Vision-backend benchmark for the advisory vision lane (ADR 0004 follow-up).

Runs candidate vision models over the owner's photographed bins and scores
each against the owner's own evidence: the item list entered at photo-drop
time and the accepted passport theme. Every call goes through the production
``VisionClient.analyze`` seam with the lane's real prompt, so results measure
the code path the service runs, not a bespoke harness path.

Validity limits (state them with any claim): the workload is this owner's
photo set, one photo per bin; recall is scored against the owner's full bin
inventory, which a single photo may only partially show, so scores compare
models relatively and are not absolute accuracy. Runs are serial to keep
local-GPU contention out of the latency numbers, and each model gets one
unscored warmup call so cold model loads are not billed to the first photo.

Benchmark results name real bin contents; they are owner data and must never
be committed to the repository.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from binkeeper.bin_vision import (
    BinVisionError,
    VisionClient,
    _build_prompt,
    _parse_vision_json,
)

_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]+")
# Tokens too generic to establish that a prediction matched an owner item.
_STOP_TOKENS: Final[frozenset[str]] = frozenset(
    {"a", "an", "and", "for", "of", "or", "the", "with", "new", "small", "large", "misc"}
)
_MIN_TOKEN_LEN: Final[int] = 3


def significant_tokens(text: str) -> frozenset[str]:
    """Lowercased alphanumeric tokens that can evidence an item match."""
    return frozenset(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) >= _MIN_TOKEN_LEN and token not in _STOP_TOKENS
    )


def parse_owner_items(content_text: str) -> tuple[str, ...]:
    """Extract the owner's item list from a photo-drop capture text.

    The capture format is ``bin AGR-003 @ site · item, item, item``; text
    without the item separator yields no items (theme-only ground truth).
    """
    _, separator, items_part = content_text.partition("·")
    if not separator:
        return ()
    items = tuple(item.strip() for item in items_part.split(",") if item.strip())
    return items


@dataclass(frozen=True)
class BenchPhoto:
    """One benchmark input: a bin photo with its owner-evidence references."""

    bin_code: str
    photo_sha256: str
    owner_theme: str
    owner_items: tuple[str, ...]


@dataclass(frozen=True)
class CallScore:
    """Scores for one model call on one photo."""

    bin_code: str
    repeat: int
    seconds: float
    json_ok: bool
    error: str | None
    theme: str | None
    predicted_labels: tuple[str, ...]
    matched_items: tuple[str, ...]
    missed_items: tuple[str, ...]
    unmatched_predictions: tuple[str, ...]
    theme_match: bool

    @property
    def recall(self) -> float | None:
        total = len(self.matched_items) + len(self.missed_items)
        if total == 0:
            return None
        return len(self.matched_items) / total

    def to_json(self) -> dict[str, object]:
        return {
            "bin_code": self.bin_code,
            "repeat": self.repeat,
            "seconds": round(self.seconds, 2),
            "json_ok": self.json_ok,
            "error": self.error,
            "theme": self.theme,
            "theme_match": self.theme_match,
            "recall": self.recall,
            "predicted_labels": list(self.predicted_labels),
            "matched_items": list(self.matched_items),
            "missed_items": list(self.missed_items),
            "unmatched_predictions": list(self.unmatched_predictions),
        }


@dataclass
class ModelReport:
    """All call scores for one candidate model."""

    model_id: str
    calls: list[CallScore] = field(default_factory=list)

    @property
    def scored_calls(self) -> list[CallScore]:
        return [call for call in self.calls if call.error is None]

    @property
    def error_count(self) -> int:
        return len(self.calls) - len(self.scored_calls)

    @property
    def json_rate(self) -> float | None:
        if not self.calls:
            return None
        return sum(1 for call in self.calls if call.json_ok) / len(self.calls)

    @property
    def mean_recall(self) -> float | None:
        recalls = [c.recall for c in self.scored_calls if c.recall is not None]
        if not recalls:
            return None
        return sum(recalls) / len(recalls)

    @property
    def theme_match_rate(self) -> float | None:
        scored = self.scored_calls
        if not scored:
            return None
        return sum(1 for call in scored if call.theme_match) / len(scored)

    @property
    def latency_range(self) -> tuple[float, float, float] | None:
        """(min, mean, max) wall seconds over non-error calls."""
        seconds = [call.seconds for call in self.scored_calls]
        if not seconds:
            return None
        return (min(seconds), sum(seconds) / len(seconds), max(seconds))

    def to_json(self) -> dict[str, object]:
        latency = self.latency_range
        return {
            "model_id": self.model_id,
            "mean_recall": self.mean_recall,
            "theme_match_rate": self.theme_match_rate,
            "json_rate": self.json_rate,
            "error_count": self.error_count,
            "latency_min_mean_max_s": [round(s, 2) for s in latency] if latency else None,
            "calls": [call.to_json() for call in self.calls],
        }


def _match_items(
    owner_items: Sequence[str], predictions: Sequence[tuple[str, frozenset[str]]]
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Match owner items to predicted items by shared significant tokens.

    Returns (matched owner items, missed owner items, unmatched predicted
    labels). Unmatched predictions may be genuinely present but unlisted, so
    they are reported, not penalized.
    """
    matched: list[str] = []
    missed: list[str] = []
    used_predictions: set[int] = set()
    for item in owner_items:
        item_tokens = significant_tokens(item)
        hit = None
        for index, (_, tokens) in enumerate(predictions):
            if item_tokens & tokens:
                hit = index
                break
        if hit is None:
            missed.append(item)
        else:
            used_predictions.add(hit)
            matched.append(item)
    unmatched = tuple(
        label for index, (label, _) in enumerate(predictions) if index not in used_predictions
    )
    return tuple(matched), tuple(missed), tuple(unmatched)


def score_raw_output(photo: BenchPhoto, raw: str, seconds: float, repeat: int) -> CallScore:
    """Score one model answer against the photo's owner evidence."""
    try:
        parsed = _parse_vision_json(raw)
    except BinVisionError:
        return CallScore(
            bin_code=photo.bin_code,
            repeat=repeat,
            seconds=seconds,
            json_ok=False,
            error=None,
            theme=None,
            predicted_labels=(),
            matched_items=(),
            missed_items=photo.owner_items,
            unmatched_predictions=(),
            theme_match=False,
        )
    predictions: list[tuple[str, frozenset[str]]] = []
    raw_items = parsed.get("items")
    if isinstance(raw_items, list):
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            label = str(entry.get("label") or "").strip()
            if not label:
                continue
            traits = entry.get("traits")
            trait_text = " ".join(str(t) for t in traits) if isinstance(traits, list) else ""
            predictions.append((label, significant_tokens(f"{label} {trait_text}")))
    matched, missed, unmatched = _match_items(photo.owner_items, predictions)
    theme = str(parsed.get("theme") or "").strip() or None
    theme_match = bool(theme and significant_tokens(theme) & significant_tokens(photo.owner_theme))
    return CallScore(
        bin_code=photo.bin_code,
        repeat=repeat,
        seconds=seconds,
        json_ok=True,
        error=None,
        theme=theme,
        predicted_labels=tuple(label for label, _ in predictions),
        matched_items=matched,
        missed_items=missed,
        unmatched_predictions=unmatched,
        theme_match=theme_match,
    )


def run_model(
    model_id: str,
    client: VisionClient,
    photos: Sequence[tuple[BenchPhoto, bytes]],
    *,
    repeats: int = 2,
    warmup_image: bytes | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ModelReport:
    """Run one model serially over all photos with an unscored warmup call."""
    report = ModelReport(model_id=model_id)
    prompt = _build_prompt(None)
    progress = on_progress or (lambda message: None)
    if warmup_image is not None:
        try:
            client.analyze(prompt, warmup_image)
        except BinVisionError as exc:
            progress(f"{model_id}: warmup failed: {exc}")
    for photo, image in photos:
        for repeat in range(repeats):
            start = time.monotonic()
            try:
                raw = client.analyze(prompt, image)
            except BinVisionError as exc:
                report.calls.append(
                    CallScore(
                        bin_code=photo.bin_code,
                        repeat=repeat,
                        seconds=time.monotonic() - start,
                        json_ok=False,
                        error=str(exc),
                        theme=None,
                        predicted_labels=(),
                        matched_items=(),
                        missed_items=photo.owner_items,
                        unmatched_predictions=(),
                        theme_match=False,
                    )
                )
                progress(f"{model_id} {photo.bin_code} r{repeat}: ERROR {exc}")
                continue
            score = score_raw_output(photo, raw, time.monotonic() - start, repeat)
            report.calls.append(score)
            recall = "-" if score.recall is None else f"{score.recall:.2f}"
            progress(
                f"{model_id} {photo.bin_code} r{repeat}: recall {recall} "
                f"theme_match {score.theme_match} {score.seconds:.1f}s"
            )
    return report


def render_summary(reports: Sequence[ModelReport]) -> str:
    """Render a markdown comparison table over all model reports."""
    lines = [
        "| model | mean recall | theme match | json ok | errors | latency s (min/mean/max) |",
        "|---|---|---|---|---|---|",
    ]

    def fmt(value: float | None) -> str:
        return "-" if value is None else f"{value:.2f}"

    for report in sorted(
        reports, key=lambda r: (r.mean_recall is not None, r.mean_recall), reverse=True
    ):
        latency = report.latency_range
        latency_text = "-" if latency is None else "/".join(f"{s:.1f}" for s in latency)
        lines.append(
            f"| {report.model_id} | {fmt(report.mean_recall)} "
            f"| {fmt(report.theme_match_rate)} | {fmt(report.json_rate)} "
            f"| {report.error_count} | {latency_text} |"
        )
    return "\n".join(lines)


def reports_to_json(reports: Sequence[ModelReport]) -> str:
    return json.dumps([report.to_json() for report in reports], indent=2)
