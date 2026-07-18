from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from binkeeper.liveness_adapter import (
    FileLivenessSource,
    LivenessAdapterError,
    StaticLivenessSource,
    UnavailableLivenessSource,
)


def test_absent_adapter_is_explicitly_unavailable() -> None:
    result = UnavailableLivenessSource().load()

    assert result.status == "unavailable"
    assert result.mentions == ()
    assert result.reason == "offline liveness export is not configured"


def test_file_adapter_reads_only_the_inert_versioned_export(tmp_path) -> None:
    export = tmp_path / "liveness.json"
    export.write_text(
        json.dumps(
            {
                "schema_version": "binkeeper.liveness-export.v1",
                "mentions": [
                    {
                        "source_kind": "capture",
                        "source_ref": "offline:1",
                        "content_text": "used the torque wrench",
                        "mentioned_at": "2026-07-01T12:00:00Z",
                        "privacy_tier": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = FileLivenessSource(export).load()

    assert result.status == "available"
    assert result.mentions[0].source_ref == "offline:1"
    assert result.mentions[0].mentioned_at == datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_file_adapter_rejects_unversioned_or_malformed_input(tmp_path) -> None:
    export = tmp_path / "liveness.json"
    export.write_text('{"mentions": []}', encoding="utf-8")

    with pytest.raises(LivenessAdapterError, match="schema_version"):
        FileLivenessSource(export).load()


def test_static_adapter_is_a_deterministic_test_seam() -> None:
    assert StaticLivenessSource(()).load().status == "available"
