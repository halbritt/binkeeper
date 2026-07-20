"""Stash-run deck routes: batch placement decisions on the phone surface (BINK-26).

The deck deals one card per undecided routing receipt. Every tap appends
exactly one placement decision over that immutable receipt: *Put it there* =
``accept`` of the recommendation, an alternative bin button = ``override``,
*Not an item* = ``not_an_item``, *None of these* = ``reject``. Abstained
receipts (the router shrugged) skip the deck into a visible pending pile whose
cards offer the same choices minus accept. Nothing places without a tap.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, TypedDict
from urllib.parse import quote

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Path as ApiPath
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from binkeeper.bin_manage import BinActionScope
from binkeeper.bin_placement import PlacementLedgerError
from binkeeper.bin_stash import StashRunError
from binkeeper.db import ServingRoleUnavailableError
from binkeeper.web.chrome import SurfaceChrome, page_response
from binkeeper.web.paths import surface_path

_LOGGER = logging.getLogger(__name__)

_DECK_DECISIONS = frozenset({"accept", "override", "reject", "not_an_item"})


class StashAlternative(TypedDict):
    """One tappable alternative destination on a card."""

    bin_code: str
    score_label: str


class StashCard(TypedDict):
    """One undecided receipt, dealt as a card."""

    routing_request_id: str
    text: str
    disposition: str  # "deck" | "pending"
    recommended_bin_code: str | None
    recommended_score_label: str
    alternatives: list[StashAlternative]
    abstain_flags: list[str]


class DecidedRow(TypedDict):
    """One already-decided receipt, for the run summary."""

    text: str
    decision_kind: str
    selected_bin_code: str | None


class StashDeckView(TypedDict):
    """Everything the deck template renders for one run."""

    stash_run_id: str
    site: str
    cards: list[StashCard]
    decided: list[DecidedRow]


class StashRunSummary(TypedDict):
    """One recent run on the start page."""

    stash_run_id: str
    site: str
    item_count: int
    undecided_count: int
    requested_at_label: str


class StashDeckLoader(Protocol):
    """Loader seam for one run's deck state."""

    def __call__(self, *, stash_run_id: str, tenant_id: str, corpus_id: str) -> StashDeckView: ...


class StashRunLister(Protocol):
    """Loader seam for the recent-runs list."""

    def __call__(self, *, tenant_id: str, corpus_id: str) -> list[StashRunSummary]: ...


class QuorumLoader(Protocol):
    """Loader seam for a site's quorum proposals (template-ready mappings)."""

    def __call__(self, *, site: str, tenant_id: str, corpus_id: str) -> list[dict[str, object]]: ...


class WavePlanLoader(Protocol):
    """Loader seam for one run's compiled wave plan (template-ready mapping)."""

    def __call__(
        self, *, stash_run_id: str, tenant_id: str, corpus_id: str
    ) -> dict[str, object]: ...


@dataclass(frozen=True)
class StashDecision:
    """One tapped verdict over one receipt."""

    routing_request_id: str
    decision_kind: str
    selected_bin_code: str | None
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class StashRunStart:
    """One submitted start form."""

    site: str
    items: list[str]
    action_id: str
    received_at: datetime


StashDecisionRecorder = Callable[[StashDecision, BinActionScope], None]
StashRunCreator = Callable[[StashRunStart, BinActionScope], str]
WaveStopCompleter = Callable[[str, str, BinActionScope], None]
QuorumFounder = Callable[[list[str], str, BinActionScope], int]
CodeSuggester = Callable[[str], str | None]


@dataclass(frozen=True)
class StashRouteConfig:
    """Dependencies for the stash deck routes."""

    base_path: str
    chrome: SurfaceChrome
    scope: BinActionScope
    sites: tuple[str, ...]
    strict_origin: Callable[[Request], None]
    load_deck: StashDeckLoader | None = None
    list_runs: StashRunLister | None = None
    create_run: StashRunCreator | None = None
    record_decision: StashDecisionRecorder | None = None
    load_wave: WavePlanLoader | None = None
    complete_stop: WaveStopCompleter | None = None
    load_quorum: QuorumLoader | None = None
    found_bin: QuorumFounder | None = None
    suggest_code: CodeSuggester | None = None


def install_stash_routes(app: FastAPI, config: StashRouteConfig) -> None:
    """Attach the stash-run deck routes to the authoring app."""
    strict_origin = config.strict_origin

    @app.get("/stash")
    def stash_start(notice: str = "") -> object:
        return _start_page(config, notice)

    @app.post("/stash")
    async def stash_create(
        request: Request,
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _create_response(request, config)

    @app.get("/stash/quorum")
    def stash_quorum(site: str = "", notice: str = "") -> object:
        return _quorum_page(config, site, notice)

    @app.post("/stash/quorum/found")
    async def stash_quorum_found(
        request: Request,
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _quorum_found_response(request, config)

    @app.get("/stash/{stash_run_id}")
    def stash_deck(
        stash_run_id: str = ApiPath(min_length=1, max_length=120),
        notice: str = "",
    ) -> object:
        return _deck_page(config, stash_run_id, notice)

    @app.post("/stash/{stash_run_id}/decide")
    async def stash_decide(
        request: Request,
        stash_run_id: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _decide_response(request, stash_run_id, config)

    @app.get("/stash/{stash_run_id}/wave")
    def stash_wave(
        stash_run_id: str = ApiPath(min_length=1, max_length=120),
        notice: str = "",
    ) -> object:
        return _wave_page(config, stash_run_id, notice)

    @app.post("/stash/{stash_run_id}/wave/complete")
    async def stash_wave_complete(
        request: Request,
        stash_run_id: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _wave_complete_response(request, stash_run_id, config)


def _start_page(config: StashRouteConfig, notice: str) -> object:
    list_runs = config.list_runs or list_recent_stash_runs
    try:
        runs = list_runs(tenant_id=config.scope.tenant_id, corpus_id=config.scope.corpus_id)
    except (psycopg.Error, ServingRoleUnavailableError):
        _LOGGER.warning("BinKeeper could not list stash runs")
        runs = []
    return page_response(
        config.chrome.render(
            "bin_stash_start.html",
            sites=list(config.sites),
            runs=runs,
            stash_action=surface_path(config.base_path, "/stash"),
            stash_url=surface_path(config.base_path, "/stash"),
            notice=_stash_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            binkeeper_section="stash",
            surface_label="Stash run",
        )
    )


async def _create_response(request: Request, config: StashRouteConfig) -> RedirectResponse:
    form = await request.form()
    raw_items = form.get("items")
    items = [
        line.strip()
        for line in (raw_items if isinstance(raw_items, str) else "").splitlines()
        if line.strip()
    ]
    start = StashRunStart(
        site=_form_text(form.get("site")),
        items=items,
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    if start.site not in config.sites or not items:
        return _start_redirect(config.base_path, "start-invalid")
    create = config.create_run or create_stash_run
    try:
        run_id = await run_in_threadpool(create, start, config.scope)
    except (StashRunError, PlacementLedgerError, psycopg.Error, RuntimeError):
        _LOGGER.warning("BinKeeper stash-run creation failed")
        return _start_redirect(config.base_path, "start-failed")
    return RedirectResponse(
        surface_path(config.base_path, f"/stash/{quote(run_id, safe='')}"),
        status_code=303,
    )


def _deck_page(config: StashRouteConfig, stash_run_id: str, notice: str) -> object:
    load_deck = config.load_deck or load_stash_deck
    try:
        view = load_deck(
            stash_run_id=stash_run_id,
            tenant_id=config.scope.tenant_id,
            corpus_id=config.scope.corpus_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="stash run not found") from None
    except (psycopg.Error, ServingRoleUnavailableError):
        _LOGGER.warning("BinKeeper could not load a stash deck")
        raise HTTPException(status_code=503, detail="stash run is unavailable") from None
    deck = [card for card in view["cards"] if card["disposition"] == "deck"]
    pending = [card for card in view["cards"] if card["disposition"] == "pending"]
    run_path = surface_path(config.base_path, f"/stash/{quote(stash_run_id, safe='')}")
    return page_response(
        config.chrome.render(
            "bin_stash_deck.html",
            **view,
            deck=deck,
            pending=pending,
            decide_action=f"{run_path}/decide",
            run_url=run_path,
            stash_url=surface_path(config.base_path, "/stash"),
            notice=_stash_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            binkeeper_section="stash",
            surface_label="Stash deck",
        )
    )


async def _decide_response(
    request: Request,
    stash_run_id: str,
    config: StashRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    decision_kind = _form_text(form.get("decision"))
    selected = _form_text(form.get("selected_bin")) or None
    if decision_kind not in _DECK_DECISIONS:
        return _deck_redirect(config.base_path, stash_run_id, "decide-invalid")
    decision = StashDecision(
        routing_request_id=_form_text(form.get("routing_request_id")),
        decision_kind=decision_kind,
        selected_bin_code=selected if decision_kind == "override" else None,
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    record = config.record_decision or record_stash_decision
    try:
        await run_in_threadpool(record, decision, config.scope)
    except (PlacementLedgerError, psycopg.Error, RuntimeError):
        _LOGGER.warning("BinKeeper stash decision failed")
        return _deck_redirect(config.base_path, stash_run_id, "decide-failed")
    return _deck_redirect(config.base_path, stash_run_id, f"decided-{decision_kind}")


def _wave_page(config: StashRouteConfig, stash_run_id: str, notice: str) -> object:
    load_wave = config.load_wave or load_wave_plan
    try:
        view = load_wave(
            stash_run_id=stash_run_id,
            tenant_id=config.scope.tenant_id,
            corpus_id=config.scope.corpus_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="stash run not found") from None
    except (psycopg.Error, ServingRoleUnavailableError):
        _LOGGER.warning("BinKeeper could not load a wave plan")
        raise HTTPException(status_code=503, detail="wave plan is unavailable") from None
    run_path = surface_path(config.base_path, f"/stash/{quote(stash_run_id, safe='')}")
    return page_response(
        config.chrome.render(
            "bin_stash_wave.html",
            **view,
            complete_action=f"{run_path}/wave/complete",
            run_url=run_path,
            stash_url=surface_path(config.base_path, "/stash"),
            notice=_stash_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            binkeeper_section="stash",
            surface_label="Wave plan",
        )
    )


async def _wave_complete_response(
    request: Request,
    stash_run_id: str,
    config: StashRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    bin_code = _form_text(form.get("bin_code"))
    if not bin_code.strip():
        return _wave_redirect(config.base_path, stash_run_id, "stop-invalid")
    complete = config.complete_stop or complete_wave_stop_io
    try:
        await run_in_threadpool(complete, stash_run_id, bin_code.strip(), config.scope)
    except (StashRunError, psycopg.Error, RuntimeError):
        _LOGGER.warning("BinKeeper wave-stop completion failed")
        return _wave_redirect(config.base_path, stash_run_id, "stop-failed")
    return _wave_redirect(config.base_path, stash_run_id, "stop-done")


def _quorum_page(config: StashRouteConfig, site: str, notice: str) -> object:
    selected = site.strip() or (config.sites[0] if config.sites else "")
    load_quorum = config.load_quorum or load_quorum_proposals
    proposals: list[dict[str, object]] = []
    if selected:
        try:
            proposals = load_quorum(
                site=selected,
                tenant_id=config.scope.tenant_id,
                corpus_id=config.scope.corpus_id,
            )
        except (psycopg.Error, ServingRoleUnavailableError):
            _LOGGER.warning("BinKeeper could not load quorum proposals")
            raise HTTPException(status_code=503, detail="proposals are unavailable") from None
    suggest = config.suggest_code or suggest_new_bin_code
    try:
        suggested = suggest(selected) if selected else None
    except (psycopg.Error, ServingRoleUnavailableError):
        suggested = None
    return page_response(
        config.chrome.render(
            "bin_stash_quorum.html",
            site=selected,
            sites=list(config.sites),
            proposals=proposals,
            suggested_code=suggested or "",
            found_action=surface_path(config.base_path, "/stash/quorum/found"),
            quorum_url=surface_path(config.base_path, "/stash/quorum"),
            stash_url=surface_path(config.base_path, "/stash"),
            notice=_stash_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            binkeeper_section="stash",
            surface_label="Bin proposals",
        )
    )


async def _quorum_found_response(request: Request, config: StashRouteConfig) -> RedirectResponse:
    form = await request.form()
    site = _form_text(form.get("site"))
    bin_code = _form_text(form.get("bin_code")).strip().upper()
    receipt_ids = [
        part.strip() for part in _form_text(form.get("receipt_ids")).split(",") if part.strip()
    ]
    if not bin_code or not receipt_ids:
        return _quorum_redirect(config.base_path, site, "found-invalid")
    found = config.found_bin or found_bin_io
    try:
        await run_in_threadpool(found, receipt_ids, bin_code, config.scope)
    except (StashRunError, PlacementLedgerError, psycopg.Error, RuntimeError):
        _LOGGER.warning("BinKeeper quorum founding failed")
        return _quorum_redirect(config.base_path, site, "found-failed")
    register = surface_path(config.base_path, f"/register?code={quote(bin_code, safe='')}")
    return RedirectResponse(register, status_code=303)


# --- production IO ------------------------------------------------------------


def create_stash_run(start: StashRunStart, scope: BinActionScope) -> str:
    """Record one stash run on the owner connection; return its id."""
    from binkeeper.bin_stash import record_stash_run
    from binkeeper.db import connect

    with connect() as conn:
        record = record_stash_run(
            conn,
            items=start.items,
            site=start.site,
            idempotency_key=start.action_id or None,
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )
    return record.stash_run_id


def record_stash_decision(decision: StashDecision, scope: BinActionScope) -> None:
    """Append one placement decision on the owner connection."""
    from binkeeper.bin_placement import PlacementDecisionAppend, record_placement_decision
    from binkeeper.db import connect

    append = PlacementDecisionAppend(
        routing_request_id=decision.routing_request_id,
        decision_kind=decision.decision_kind,  # type: ignore[arg-type]
        selected_bin_code=decision.selected_bin_code,
        actor="owner",
        decided_at=decision.received_at,
        idempotency_key=decision.action_id or None,
        tenant_id=scope.tenant_id,
        corpus_id=scope.corpus_id,
    )
    with connect() as conn:
        record_placement_decision(conn, append)


def load_stash_deck(*, stash_run_id: str, tenant_id: str, corpus_id: str) -> StashDeckView:
    """Load one run's receipts and their latest decisions. Read-only."""
    from binkeeper.db import connect

    with connect(role="serving") as conn:
        run = conn.execute(
            """
            SELECT id::text, site FROM stash_runs
            WHERE tenant_id = %s AND corpus_id = %s AND id::text = %s
            """,
            (tenant_id, corpus_id, stash_run_id),
        ).fetchone()
        if run is None:
            raise LookupError(f"unknown stash run {stash_run_id!r}")
        rows = conn.execute(
            """
            SELECT r.id::text, r.input_text, r.route_result_json,
                   d.decision_kind, d.selected_bin_code
            FROM bin_routing_requests r
            LEFT JOIN LATERAL (
                SELECT decision_kind, selected_bin_code
                FROM bin_placement_decisions d
                WHERE d.tenant_id = r.tenant_id
                  AND d.corpus_id = r.corpus_id
                  AND d.routing_request_id = r.id
                ORDER BY d.seq DESC
                LIMIT 1
            ) d ON TRUE
            WHERE r.tenant_id = %s AND r.corpus_id = %s AND r.stash_run_id = %s
            ORDER BY r.seq
            """,
            (tenant_id, corpus_id, stash_run_id),
        ).fetchall()
    cards: list[StashCard] = []
    decided: list[DecidedRow] = []
    for row in rows:
        text = str(row[1])
        if row[3] is not None:
            decided.append(
                DecidedRow(
                    text=text,
                    decision_kind=str(row[3]),
                    selected_bin_code=str(row[4]) if row[4] else None,
                )
            )
            continue
        cards.append(_card_from_route(str(row[0]), text, row[2]))
    return StashDeckView(
        stash_run_id=str(run[0]),
        site=str(run[1]),
        cards=cards,
        decided=decided,
    )


def _card_from_route(request_id: str, text: str, route_result: object) -> StashCard:
    result: Mapping[str, object] = route_result if isinstance(route_result, Mapping) else {}
    recommended = result.get("recommended_bin_code")
    top = result.get("top_candidate")
    top_map: Mapping[str, object] = top if isinstance(top, Mapping) else {}
    alternatives_raw = result.get("alternatives")
    alternatives: list[StashAlternative] = []
    if isinstance(alternatives_raw, list):
        for entry in alternatives_raw[:3]:
            if isinstance(entry, Mapping) and entry.get("bin_code"):
                alternatives.append(
                    StashAlternative(
                        bin_code=str(entry["bin_code"]),
                        score_label=_score_label(entry.get("total_score")),
                    )
                )
    flags_raw = result.get("abstain_flags")
    flags = [str(flag) for flag in flags_raw] if isinstance(flags_raw, list) else []
    return StashCard(
        routing_request_id=request_id,
        text=text,
        disposition="deck" if recommended else "pending",
        recommended_bin_code=str(recommended) if recommended else None,
        recommended_score_label=_score_label(top_map.get("total_score")),
        alternatives=alternatives,
        abstain_flags=flags,
    )


def _score_label(score: object) -> str:
    try:
        return f"{round(float(score) * 100)}%"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"


def list_recent_stash_runs(*, tenant_id: str, corpus_id: str) -> list[StashRunSummary]:
    """List the newest runs with their undecided counts. Read-only."""
    from binkeeper.db import connect

    with connect(role="serving") as conn:
        rows = conn.execute(
            """
            SELECT s.id::text, s.site, s.item_count, s.requested_at,
                   count(r.id) FILTER (WHERE d.id IS NULL) AS undecided
            FROM stash_runs s
            LEFT JOIN bin_routing_requests r ON r.stash_run_id = s.id
            LEFT JOIN LATERAL (
                SELECT id FROM bin_placement_decisions d
                WHERE d.routing_request_id = r.id LIMIT 1
            ) d ON TRUE
            WHERE s.tenant_id = %s AND s.corpus_id = %s
            GROUP BY s.id, s.site, s.item_count, s.requested_at, s.seq
            ORDER BY s.seq DESC
            LIMIT 5
            """,
            (tenant_id, corpus_id),
        ).fetchall()
    return [
        StashRunSummary(
            stash_run_id=str(row[0]),
            site=str(row[1]),
            item_count=int(row[2]),
            undecided_count=int(row[4]),
            requested_at_label=row[3].strftime("%Y-%m-%d %H:%M") if row[3] else "",
        )
        for row in rows
    ]


def load_wave_plan(*, stash_run_id: str, tenant_id: str, corpus_id: str) -> dict[str, object]:
    """Compile the wave plan on the serving role. Read-only."""
    from binkeeper.bin_stash import wave_plan_for_run
    from binkeeper.db import connect

    with connect(role="serving") as conn:
        plan = wave_plan_for_run(
            conn, stash_run_id=stash_run_id, tenant_id=tenant_id, corpus_id=corpus_id
        )
    return {
        "stash_run_id": plan.stash_run_id,
        "site": plan.site,
        "stops": [stop.to_json() for stop in plan.stops],
        "completed_count": plan.completed_count,
    }


def complete_wave_stop_io(stash_run_id: str, bin_code: str, scope: BinActionScope) -> None:
    """Append one wave-stop completion capture on the owner connection."""
    from binkeeper.bin_stash import complete_wave_stop
    from binkeeper.db import connect

    with connect() as conn:
        complete_wave_stop(
            conn,
            stash_run_id=stash_run_id,
            bin_code=bin_code,
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )


def load_quorum_proposals(*, site: str, tenant_id: str, corpus_id: str) -> list[dict[str, object]]:
    """Cluster founder-eligible orphans on the serving role. Read-only."""
    from binkeeper.bin_stash import quorum_proposals
    from binkeeper.db import connect

    with connect(role="serving") as conn:
        clusters = quorum_proposals(conn, site=site, tenant_id=tenant_id, corpus_id=corpus_id)
    return [cluster.to_json() for cluster in clusters]


def found_bin_io(receipt_ids: list[str], bin_code: str, scope: BinActionScope) -> int:
    """Write the founding decisions on the owner connection."""
    from binkeeper.bin_stash import found_bin_from_cluster
    from binkeeper.db import connect

    with connect() as conn:
        return found_bin_from_cluster(
            conn,
            receipt_ids=receipt_ids,
            bin_code=bin_code,
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )


def suggest_new_bin_code(site: str) -> str | None:
    """Suggest the next free code for a site prefix (registration helper)."""
    from binkeeper.bin_photo_web import _suggest_bin_code

    return _suggest_bin_code(site)


def _quorum_redirect(base_path: str, site: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, "/stash/quorum")
    return RedirectResponse(
        f"{path}?site={quote(site, safe='')}&notice={quote(notice, safe='')}", status_code=303
    )


def _wave_redirect(base_path: str, stash_run_id: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, f"/stash/{quote(stash_run_id, safe='')}/wave")
    return RedirectResponse(f"{path}?notice={quote(notice, safe='')}", status_code=303)


def _start_redirect(base_path: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, "/stash")
    return RedirectResponse(f"{path}?notice={quote(notice, safe='')}", status_code=303)


def _deck_redirect(base_path: str, stash_run_id: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, f"/stash/{quote(stash_run_id, safe='')}")
    return RedirectResponse(f"{path}?notice={quote(notice, safe='')}", status_code=303)


def _form_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _stash_notice(key: str) -> dict[str, str] | None:
    notices = {
        "start-invalid": ("error", "Pick a known site and add at least one item line."),
        "start-failed": ("error", "The run was not recorded. Try again."),
        "decide-invalid": ("error", "That verdict is not one the deck offers."),
        "decide-failed": ("error", "Nothing was recorded. Try again."),
        "decided-accept": ("ok", "Recorded — put it in the recommended bin."),
        "decided-override": ("ok", "Recorded your pick over the recommendation."),
        "decided-reject": ("info", "Recorded the rejection. The item stays unplaced."),
        "decided-not_an_item": ("info", "Recorded — not an item. Card dismissed."),
        "stop-invalid": ("error", "That stop is not on this wave."),
        "stop-failed": ("error", "The stop was not recorded. Try again."),
        "stop-done": ("ok", "Stop recorded — the items joined the bin's contents history."),
        "found-invalid": ("error", "A bin code and the founding items are required."),
        "found-failed": ("error", "The founding decisions were not recorded. Try again."),
    }
    found = notices.get(key)
    return {"kind": found[0], "message": found[1]} if found else None
