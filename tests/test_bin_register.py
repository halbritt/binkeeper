"""RFC 0088 registration guards and append-only capture behavior."""

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from binkeeper.bin_register import BinRegisterError, register_bin


def test_register_bin_rejects_empty_bin_code() -> None:
    # Guards run before any DB access, so conn is never touched.
    with pytest.raises(BinRegisterError, match="bin_code"):
        register_bin(None, bin_code="  ", site="alameda-garage")  # type: ignore[arg-type]


def test_register_bin_rejects_empty_site() -> None:
    with pytest.raises(BinRegisterError, match="site"):
        register_bin(None, bin_code="AGR-001", site="")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("theme", "expected_profile"),
    [
        ("  Precision tools  ", {"theme": "Precision tools"}),
        ("   ", None),
    ],
)
def test_registration_capture_records_only_a_nonblank_reviewed_theme(
    conn: psycopg.Connection,
    theme: str,
    expected_profile: dict[str, str] | None,
) -> None:
    result = register_bin(
        conn,
        bin_code="AGR-014",
        site="alameda-garage",
        theme=theme,
        observed_at=datetime(2026, 7, 13, tzinfo=UTC),
    )

    row = conn.execute(
        "SELECT raw_payload->'metadata' FROM captures WHERE id = %s",
        (result.capture_id,),
    ).fetchone()
    assert row is not None
    metadata = row[0]
    assert isinstance(metadata, dict)
    assert metadata.get("bin_profile") == expected_profile
    if expected_profile is None:
        assert "bin_profile" not in metadata
