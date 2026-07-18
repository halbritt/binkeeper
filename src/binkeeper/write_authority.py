"""One process-wide authority gate for BinKeeper mutation surfaces."""

from __future__ import annotations

import os


class WriterFrozenError(RuntimeError):
    """Raised when a caller attempts mutation outside the cutover authority window."""


def writes_enabled() -> bool:
    """Return whether this process has explicit standalone writer authority."""
    return os.environ.get("BINKEEPER_WRITES_ENABLED", "0") == "1"


def require_writer_authority() -> None:
    """Refuse mutation unless the standalone writer gate is explicitly open."""
    if not writes_enabled():
        raise WriterFrozenError("BinKeeper writer is frozen")
