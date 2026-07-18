from __future__ import annotations

import pytest

from binkeeper.migrations import migration_names


@pytest.mark.migration
def test_structural_scaffold_has_no_database_migrations() -> None:
    assert migration_names() == ()
