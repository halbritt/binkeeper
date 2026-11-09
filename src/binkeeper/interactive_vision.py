"""Shared, nonwaiting admission for one interactive server process."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock
from time import perf_counter

from binkeeper.bin_vision import BinVisionError, VisionClient


class VisionBusy(BinVisionError):
    """Accepted originals can be reviewed manually or retried from Saved photos."""


class VisionBudget:
    def __init__(self, capacity: int = 3) -> None:
        self._available = capacity
        self._lock = Lock()

    @contextmanager
    def admit(self, workers: int) -> Iterator[None]:
        with self._lock:
            if workers > self._available:
                raise VisionBusy(
                    "Photo analysis is busy. "
                    "Review the entered details now or retry from Saved photos."
                )
            self._available -= workers
        try:
            yield
        finally:
            with self._lock:
                self._available += workers


INTERACTIVE_VISION_BUDGET = VisionBudget()


class TimedVisionClient:
    """Measure the union of active call intervals, without storing inputs/output."""

    def __init__(self, client: VisionClient) -> None:
        self.client = client
        self.model = client.model
        self._intervals: list[tuple[float, float]] = []
        self._lock = Lock()

    def analyze(self, prompt: str, image: bytes) -> str:
        start = perf_counter()
        try:
            return self.client.analyze(prompt, image)
        finally:
            with self._lock:
                self._intervals.append((start, perf_counter()))

    @property
    def active_seconds(self) -> float:
        total = 0.0
        end = 0.0
        for start, stop in sorted(self._intervals):
            total += max(0.0, stop - max(start, end))
            end = max(end, stop)
        return total
