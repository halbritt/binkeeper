"""Offline-only adapter contract for optional transcript liveness evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

LIVENESS_EXPORT_SCHEMA = "binkeeper.liveness-export.v1"


class LivenessAdapterError(ValueError):
    """An inert liveness export is malformed or unreadable."""


@dataclass(frozen=True)
class LivenessMention:
    source_kind: str
    source_ref: str
    content_text: str
    mentioned_at: datetime
    privacy_tier: int = 1


@dataclass(frozen=True)
class LivenessInput:
    status: Literal["available", "unavailable"]
    mentions: tuple[LivenessMention, ...]
    reason: str | None = None


class LivenessSource(Protocol):
    def load(self) -> LivenessInput: ...


class UnavailableLivenessSource:
    def load(self) -> LivenessInput:
        return LivenessInput(
            status="unavailable",
            mentions=(),
            reason="offline liveness export is not configured",
        )


class StaticLivenessSource:
    """Deterministic in-process adapter used by tests and local callers."""

    def __init__(self, mentions: tuple[LivenessMention, ...]) -> None:
        self._mentions = mentions

    def load(self) -> LivenessInput:
        return LivenessInput(status="available", mentions=self._mentions)


class FileLivenessSource:
    """Read a versioned inert JSON export from an explicit local path."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def load(self) -> LivenessInput:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise LivenessAdapterError(f"liveness export not found at {self._path}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise LivenessAdapterError("liveness export is not valid local JSON") from exc
        if not isinstance(value, dict) or value.get("schema_version") != LIVENESS_EXPORT_SCHEMA:
            raise LivenessAdapterError(f"schema_version must be {LIVENESS_EXPORT_SCHEMA}")
        raw_mentions = value.get("mentions")
        if not isinstance(raw_mentions, list):
            raise LivenessAdapterError("mentions must be a list")
        return LivenessInput(
            status="available",
            mentions=tuple(_mention(item) for item in raw_mentions),
        )


def _mention(value: object) -> LivenessMention:
    if not isinstance(value, dict):
        raise LivenessAdapterError("each mention must be an object")
    source_kind = _text(value.get("source_kind"), "source_kind")
    source_ref = _text(value.get("source_ref"), "source_ref")
    content_text = _text(value.get("content_text"), "content_text")
    mentioned = _text(value.get("mentioned_at"), "mentioned_at")
    try:
        mentioned_at = datetime.fromisoformat(mentioned.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise LivenessAdapterError("mentioned_at must be RFC3339") from exc
    privacy_tier = value.get("privacy_tier", 1)
    if not isinstance(privacy_tier, int) or isinstance(privacy_tier, bool) or privacy_tier < 0:
        raise LivenessAdapterError("privacy_tier must be a non-negative integer")
    return LivenessMention(source_kind, source_ref, content_text, mentioned_at, privacy_tier)


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LivenessAdapterError(f"{field} must be non-empty text")
    return value.strip()
