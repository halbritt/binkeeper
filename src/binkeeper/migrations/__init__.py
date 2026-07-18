"""BinKeeper-owned migration resources."""

from __future__ import annotations

from importlib import resources


def migration_names() -> tuple[str, ...]:
    """Return packaged SQL migrations in lexical order."""
    root = resources.files("binkeeper.migrations")
    return tuple(sorted(entry.name for entry in root.iterdir() if entry.name.endswith(".sql")))
