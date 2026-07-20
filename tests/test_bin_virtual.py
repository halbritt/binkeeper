"""BINK-34 tests for virtual bins (saved queries over physical bins)."""

from __future__ import annotations

import argparse

import psycopg
import pytest

from binkeeper.bin_virtual import (
    BinVirtualError,
    define_virtual_bin,
    list_virtual_bins,
    load_virtual_definitions,
    matching_virtual_names,
    virtual_members,
)
from binkeeper.cli import execute
from tests.test_bin_stash import _passport


def test_virtual_members_require_every_query_token() -> None:
    soldering = _passport("AGR-020", theme="soldering iron tips and flux")
    fasteners = _passport("AGR-021", theme="fasteners hardware")
    members = virtual_members("soldering flux", [fasteners, soldering])
    assert [member.bin_code for member in members] == ["AGR-020"]
    assert virtual_members("", [soldering]) == ()


def test_define_list_and_retire_round_trip(conn: psycopg.Connection) -> None:
    assert define_virtual_bin(conn, name="Soldering", query="soldering", action_id="v1") is True
    assert define_virtual_bin(conn, name="Soldering", query="soldering", action_id="v1") is False
    assert load_virtual_definitions(conn) == {"soldering": "soldering"}

    bins = list_virtual_bins(conn)
    assert [bin.name for bin in bins] == ["soldering"]
    assert bins[0].members == ()  # empty corpus: computed, not stored

    assert define_virtual_bin(conn, name="soldering", query="", action_id="v2") is True
    assert load_virtual_definitions(conn) == {}
    assert list_virtual_bins(conn) == []


def test_define_requires_name_and_action(conn: psycopg.Connection) -> None:
    with pytest.raises(BinVirtualError):
        define_virtual_bin(conn, name="  ", query="x", action_id="v1")
    with pytest.raises(BinVirtualError):
        define_virtual_bin(conn, name="x", query="x", action_id=" ")


def test_search_payload_names_matching_virtual_bins(conn: psycopg.Connection) -> None:
    define_virtual_bin(conn, name="soldering", query="soldering flux", action_id="v3")
    payload = execute(
        argparse.Namespace(
            command="bin-search",
            query="soldering iron",
            limit=20,
            tenant="personal",
            corpus="personal",
        ),
        conn,
    )
    assert payload["virtual_bins"] == ["soldering"]
    miss = execute(
        argparse.Namespace(
            command="bin-search", query="yoga mat", limit=20, tenant="personal", corpus="personal"
        ),
        conn,
    )
    assert miss["virtual_bins"] == []


def test_matching_names_is_pure_and_token_based() -> None:
    definitions = {"soldering": "soldering flux", "camping": "stove fuel"}
    assert matching_virtual_names("flux capacitor", definitions) == ["soldering"]
    assert matching_virtual_names("unrelated", definitions) == []
