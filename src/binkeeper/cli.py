"""Standalone command line for physical inventory behavior."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

import psycopg

from binkeeper.bin_anchor import print_anchor_label
from binkeeper.bin_inventory import arrive_all, bin_belief, bin_where, record_event, trip_status
from binkeeper.bin_passport import bin_passport
from binkeeper.bin_placement import PlacementDecisionAppend, record_placement_decision
from binkeeper.bin_route import bin_route
from binkeeper.bin_stash import record_stash_run, stash_route_batch
from binkeeper.bin_sweep import BIN_SWEEP_DEFAULT_LIMIT, bin_sweep
from binkeeper.database import connect
from binkeeper.search import search_inventory
from binkeeper.write_authority import require_writer_authority


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="binkeeper", description="Local physical inventory")
    subparsers = parser.add_subparsers(dest="command", required=True)
    trip = subparsers.add_parser("trip-scan", help="append one move or trip event")
    trip.add_argument(
        "--action",
        required=True,
        choices=(
            "place",
            "open",
            "load",
            "arrive",
            "close",
            "confirm",
            "contradict",
            "fetch",
            "not_found",
            "browse",
        ),
    )
    trip.add_argument("--bin", dest="bin_code")
    trip.add_argument("--trip", dest="trip_id")
    trip.add_argument("--site")
    trip.add_argument("--from-site")
    trip.add_argument("--to-site")
    trip.add_argument("--occurred-at")
    trip.add_argument("--source-label", default="manual")
    trip.add_argument("--idempotency-key")
    _scope(trip)

    where = subparsers.add_parser("bin-where", help="fold a bin's current location")
    where.add_argument("bin_code")
    _scope(where)
    belief = subparsers.add_parser("bin-belief", help="show location confidence and abstention")
    belief.add_argument("bin_code")
    _scope(belief)
    passport = subparsers.add_parser("bin-passport", help="render a read-only bin passport")
    passport.add_argument("bin_code")
    _scope(passport)
    search = subparsers.add_parser("bin-search", help="search local inventory evidence")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    _scope(search)
    route = subparsers.add_parser("bin-route", help="route typed item text over passports")
    route.add_argument("--text", required=True)
    route.add_argument("--site", required=True)
    _scope(route)
    decision = subparsers.add_parser(
        "bin-placement-decision", help="append an owner decision over a route receipt"
    )
    decision.add_argument("--request-id", required=True)
    decision.add_argument(
        "--decision",
        required=True,
        choices=("accept", "reject", "override", "split", "merge", "not_an_item", "create_new_bin"),
    )
    decision.add_argument("--selected-bin")
    decision.add_argument("--actor", default="owner")
    decision.add_argument("--reason")
    decision.add_argument("--idempotency-key")
    _scope(decision)
    sweep = subparsers.add_parser("bin-sweep", help="list the present site's stalest bins")
    sweep.add_argument("--site")
    sweep.add_argument("--limit", type=int)
    _scope(sweep)
    stash = subparsers.add_parser(
        "bin-stash-route", help="route a batch of item texts and print the wave plan"
    )
    stash.add_argument("--site", required=True)
    stash.add_argument("--item", action="append", dest="items", default=[])
    stash.add_argument("--items-file", help="one item per line; '-' reads stdin")
    _scope(stash)
    run = subparsers.add_parser(
        "bin-stash-run", help="record a stash run: batch receipts over one routing pass"
    )
    run.add_argument("--site", required=True)
    run.add_argument("--item", action="append", dest="items", default=[])
    run.add_argument("--items-file", help="one item per line; '-' reads stdin")
    run.add_argument("--idempotency-key")
    _scope(run)
    anchor = subparsers.add_parser(
        "bin-anchor-label", help="print one LOC- sub-location anchor label (reviewed intent)"
    )
    anchor.add_argument("--code", required=True)
    anchor.add_argument("--action-id", dest="action_id", required=True)
    anchor.add_argument("--text", dest="label_text")
    _scope(anchor)
    return parser


def _scope(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tenant", default="personal")
    parser.add_argument("--corpus", default="personal")


def execute(args: argparse.Namespace, conn: psycopg.Connection) -> dict[str, Any]:
    if args.command in {
        "trip-scan",
        "bin-placement-decision",
        "bin-stash-run",
        "bin-anchor-label",
    }:
        require_writer_authority()
    common = {"tenant_id": args.tenant, "corpus_id": args.corpus}
    if args.command == "trip-scan":
        if args.action == "arrive" and args.bin_code is None:
            if not args.trip_id:
                raise ValueError("trip-scan arrive without --bin requires --trip")
            arrivals = arrive_all(
                conn,
                args.trip_id,
                site=args.site,
                occurred_at=_datetime(args.occurred_at),
                source_label=args.source_label,
                **common,
            )
            return {
                "arrived": [arrival.to_json() for arrival in arrivals],
                "trip": trip_status(conn, args.trip_id, **common).to_json(),
            }
        result = record_event(
            conn,
            event_kind=args.action,
            trip_id=args.trip_id,
            bin_code=args.bin_code,
            from_site=args.from_site,
            to_site=args.to_site,
            site=args.site,
            occurred_at=_datetime(args.occurred_at),
            source_label=args.source_label,
            idempotency_key=args.idempotency_key,
            **common,
        )
        payload = result.to_json()
        if args.trip_id:
            payload["trip"] = trip_status(conn, args.trip_id, **common).to_json()
        return payload
    if args.command == "bin-where":
        return bin_where(conn, args.bin_code, **common).to_json()
    if args.command == "bin-belief":
        return bin_belief(conn, args.bin_code, **common).to_json()
    if args.command == "bin-passport":
        return bin_passport(conn, args.bin_code, **common).to_json()
    if args.command == "bin-search":
        return search_inventory(conn, args.query, limit=args.limit, **common).to_json()
    if args.command == "bin-route":
        return bin_route(conn, text=args.text, site=args.site, **common).to_json()
    if args.command == "bin-placement-decision":
        request = PlacementDecisionAppend(
            routing_request_id=args.request_id,
            decision_kind=args.decision,
            selected_bin_code=args.selected_bin,
            actor=args.actor,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
            **common,
        )
        return record_placement_decision(conn, request).to_json()
    if args.command == "bin-sweep":
        return bin_sweep(
            conn,
            site=args.site,
            limit=args.limit if args.limit is not None else BIN_SWEEP_DEFAULT_LIMIT,
            **common,
        ).to_json()
    if args.command == "bin-stash-route":
        items = _collect_items(args)
        if not items:
            raise ValueError("bin-stash-route needs --item or --items-file")
        return stash_route_batch(conn, items=items, site=args.site, **common).to_json()
    if args.command == "bin-stash-run":
        items = _collect_items(args)
        if not items:
            raise ValueError("bin-stash-run needs --item or --items-file")
        return record_stash_run(
            conn,
            items=items,
            site=args.site,
            idempotency_key=args.idempotency_key,
            **common,
        ).to_json()
    if args.command == "bin-anchor-label":
        return print_anchor_label(
            conn,
            anchor_code=args.code,
            action_id=args.action_id,
            label_text=args.label_text,
            **common,
        ).to_json()
    raise ValueError(f"unsupported command {args.command!r}")


def _collect_items(args: argparse.Namespace) -> list[str]:
    items = list(args.items)
    if args.items_file == "-":
        items.extend(line.strip() for line in sys.stdin if line.strip())
    elif args.items_file:
        with open(args.items_file, encoding="utf-8") as stream:
            items.extend(line.strip() for line in stream if line.strip())
    return items


def _datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def main() -> None:
    args = build_parser().parse_args()
    read_only = args.command in {
        "bin-where",
        "bin-belief",
        "bin-passport",
        "bin-search",
        "bin-route",
        "bin-sweep",
        "bin-stash-route",
    }
    with connect(role="serving" if read_only else "owner") as conn:
        print(json.dumps(execute(args, conn), sort_keys=True))
