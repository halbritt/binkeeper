from __future__ import annotations

import pytest

from binkeeper.migrations import migration_names


@pytest.mark.migration
def test_initial_persistence_migration_is_packaged() -> None:
    assert migration_names() == ("001_initial_persistence.sql",)
