"""BinKeeper-owned absolute confidence gate."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AbstainDecision:
    abstained: bool
    top1: float
    floor: float
    threshold: float
    shortfall: float


def absolute_gate(
    top1: float, floor: float, *, m_abs: float = 0.0, ci: float = 0.0
) -> AbstainDecision:
    """Abstain when a confidence value is below the absolute floor."""
    threshold = floor + m_abs * ci
    return AbstainDecision(
        abstained=top1 < threshold,
        top1=top1,
        floor=floor,
        threshold=threshold,
        shortfall=max(0.0, threshold - top1),
    )
