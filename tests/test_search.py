from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg

from binkeeper.bin_manage import BinProfileUpdate, update_bin_profile
from binkeeper.bin_register import register_bin
from binkeeper.cli import build_parser, execute
from binkeeper.search import search_inventory


def _inventory_history(conn: psycopg.Connection) -> None:
    registered = datetime(2026, 7, 1, 10, tzinfo=UTC)
    register_bin(
        conn,
        bin_code="TST-042",
        site="shop",
        theme="Painting",
        contents_text="rollers and trays",
        observed_at=registered,
    )
    update_bin_profile(
        conn,
        BinProfileUpdate(
            bin_code="TST-042",
            theme="Power tools",
            contents="cordless drills and impact drivers",
            home_site="shop",
            action_id="95fb4740-e8dc-4ef4-961c-744309a510e4",
            observed_at=registered + timedelta(days=1),
        ),
    )


def test_search_finds_code_current_contents_and_reviewed_theme(
    conn: psycopg.Connection,
) -> None:
    _inventory_history(conn)

    by_code = search_inventory(conn, "tst-042")
    by_contents = search_inventory(conn, "impact drivers")
    by_theme = search_inventory(conn, "power tools")

    assert by_code.status == "ok"
    assert by_code.hits[0].match_kind == "exact"
    assert by_code.hits[0].bin_code == "TST-042"
    assert by_contents.hits[0].matched_fields == ("current_contents",)
    assert by_theme.hits[0].matched_fields == ("theme",)
    assert all(
        hit.evidence_refs for result in (by_code, by_contents, by_theme) for hit in result.hits
    )


def test_search_surfaces_historical_evidence_without_claiming_it_is_current(
    conn: psycopg.Connection,
) -> None:
    _inventory_history(conn)

    result = search_inventory(conn, "painting")

    assert result.status == "ok"
    assert [(hit.bin_code, hit.is_current) for hit in result.hits] == [("TST-042", False)]
    assert result.hits[0].matched_fields == ("historical_theme",)
    assert result.hits[0].evidence_refs[0].startswith("capture_evidence:")


def test_search_reports_honest_no_result(conn: psycopg.Connection) -> None:
    _inventory_history(conn)

    result = search_inventory(conn, "bananas")

    assert result.status == "no_results"
    assert result.hits == ()


def test_cli_exposes_the_same_owned_search_contract(conn: psycopg.Connection) -> None:
    _inventory_history(conn)
    args = build_parser().parse_args(["bin-search", "impact drivers"])

    result = execute(args, conn)

    assert result["status"] == "ok"
    assert result["hits"][0]["bin_code"] == "TST-042"


def test_search_is_local_lexical_code_without_network_or_model_imports() -> None:
    import inspect

    import binkeeper.search as search_module

    source = inspect.getsource(search_module)
    assert "urllib" not in source
    assert "requests" not in source
    assert "embedding" not in source
    assert "segments" not in source
