from __future__ import annotations

import csv
from pathlib import Path


def test_parity_inventory_accounts_for_every_baseline_test_once() -> None:
    with Path("docs/test-parity.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    node_ids = [row["baseline_node_id"] for row in rows]
    assert len(node_ids) == 295
    assert len(set(node_ids)) == 295
    assert {row["classification"] for row in rows} <= {
        "ported",
        "replaced",
        "deferred",
        "engram-compatibility",
    }
