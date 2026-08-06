"""Existing-bin management routes and owner-side I/O for BinKeeper."""

from __future__ import annotations

import hashlib
import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol, TypedDict
from urllib.parse import quote
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi import Path as ApiPath
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from binkeeper.bin_inventory import BinInventoryError
from binkeeper.bin_label import BinLabelError
from binkeeper.bin_manage import (
    BinActionScope,
    BinLabelPrintIntent,
    BinManageError,
    BinManageNotFoundError,
    BinPhotoAddition,
    BinPlacement,
    BinProfileUpdate,
)
from binkeeper.bin_photo_media import BinPhotoMediaError, validate_bin_photo
from binkeeper.blob_vault import BlobVaultError
from binkeeper.db import ServingRoleUnavailableError
from binkeeper.personal_memory import CaptureResult, PersonalMemoryError
from binkeeper.web.chrome import SurfaceChrome, page_response
from binkeeper.web.paths import surface_path

_LOGGER = logging.getLogger(__name__)
PrintOutcome = Literal["sent", "replayed", "failed", "unknown"]
OriginCheck = Callable[[Request], None]


class ManageView(TypedDict):
    """Owner-safe current fields rendered by the management page."""

    bin_code: str
    theme: str
    contents: str
    home_site: str
    current_site: str
    catalog_photo_url: str | None
    container_code: str
    containment_path: tuple[str, ...]
    contained_bin_codes: tuple[str, ...]
    container_options: tuple[str, ...]


class ManageLoader(Protocol):
    """Exact loader seam used by the synthetic browser harness."""

    def __call__(
        self,
        *,
        bin_code: str,
        tenant_id: str,
        corpus_id: str,
    ) -> ManageView: ...


class TriageCandidate(TypedDict):
    """One same-site bin worth checking while the owner is already searching."""

    bin_code: str
    confidence_label: str
    stale: bool


class TriageLoader(Protocol):
    """Loader seam for the not-found triage candidate list."""

    def __call__(
        self,
        *,
        bin_code: str,
        site: str,
        tenant_id: str,
        corpus_id: str,
    ) -> list[TriageCandidate]: ...


RetrievalOutcome = Literal["fetch", "not_found", "confirm"]


@dataclass(frozen=True)
class RetrievalAction:
    """One owner retrieval verdict destined for the append-only trip ledger."""

    bin_code: str
    site: str
    outcome: RetrievalOutcome
    action_id: str
    received_at: datetime


RetrievalRecorder = Callable[["RetrievalAction", BinActionScope], None]


@dataclass(frozen=True)
class ManageRouteConfig:
    """Dependencies shared by the contextual management routes."""

    base_path: str
    chrome: SurfaceChrome
    scope: BinActionScope
    sites: tuple[str, ...]
    load_view: ManageLoader
    strict_origin: OriginCheck
    load_triage: TriageLoader | None = None
    record_retrieval: RetrievalRecorder | None = None


@dataclass(frozen=True)
class ProfileAction:
    bin_code: str
    theme: str
    contents: str
    home_site: str
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class PhotoAction:
    bin_code: str
    image: bytes
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class LocationAction:
    bin_code: str
    site: str
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class ContainmentAction:
    bin_code: str
    event_kind: str
    container_code: str
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class PrintAction:
    bin_code: str
    action_id: str
    received_at: datetime


@dataclass(frozen=True)
class PreparedLabel:
    """Current saved label content and its configured local queue."""

    bin_code: str
    queue: str
    tspl: str


def install_manage_routes(app: FastAPI, config: ManageRouteConfig) -> None:
    """Attach contextual existing-bin routes to the authoring app."""
    strict_origin = config.strict_origin

    @app.get("/manage/{bin_code}")
    def manage_bin(
        bin_code: str = ApiPath(min_length=1, max_length=120),
        notice: str = "",
    ) -> object:
        return _manage_page(config, bin_code, notice)

    @app.post("/manage/{bin_code}/profile")
    async def manage_profile(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _profile_response(request, bin_code, config)

    @app.post("/manage/{bin_code}/photo")
    async def manage_photo(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _photo_response(request, bin_code, config)

    @app.post("/manage/{bin_code}/location")
    async def manage_location(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _location_response(request, bin_code, config)

    @app.post("/manage/{bin_code}/containment")
    async def manage_containment(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _containment_response(request, bin_code, config)

    @app.post("/manage/{bin_code}/print")
    async def manage_print(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _print_response(request, bin_code, config)

    @app.post("/manage/{bin_code}/not-found")
    async def manage_not_found(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _not_found_response(request, bin_code, config)

    @app.get("/manage/{bin_code}/triage")
    def manage_triage(
        bin_code: str = ApiPath(min_length=1, max_length=120),
        notice: str = "",
    ) -> object:
        return _triage_page(config, bin_code, notice)

    @app.post("/manage/{bin_code}/triage/mark")
    async def manage_triage_mark(
        request: Request,
        bin_code: str = ApiPath(min_length=1, max_length=120),
        _origin: None = Depends(strict_origin),
    ) -> RedirectResponse:
        return await _triage_mark_response(request, bin_code, config)


def _manage_page(config: ManageRouteConfig, bin_code: str, notice: str) -> object:
    try:
        view = config.load_view(
            bin_code=bin_code,
            tenant_id=config.scope.tenant_id,
            corpus_id=config.scope.corpus_id,
        )
    except BinManageNotFoundError:
        raise HTTPException(status_code=404, detail="bin not found") from None
    except (psycopg.Error, ServingRoleUnavailableError):
        _LOGGER.warning("BinKeeper could not load a management page")
        raise HTTPException(status_code=503, detail="bin details are unavailable") from None
    action_urls = _action_urls(config.base_path, bin_code)
    action_ids = {f"{name.removesuffix('_action')}_action_id": str(uuid4()) for name in action_urls}
    return page_response(
        config.chrome.render(
            "bin_manage.html",
            **view,
            **action_urls,
            **action_ids,
            all_sites=list(config.sites),
            notice=_manage_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            stash_url=surface_path(config.base_path, "/stash"),
            binkeeper_section="catalog",
            surface_label="Manage bin",
        )
    )


async def _profile_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    action = ProfileAction(
        bin_code=bin_code,
        theme=_form_text(form.get("theme")),
        contents=_form_text(form.get("contents")),
        home_site=_form_text(form.get("home_site")),
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    notice = "profile-saved"
    try:
        await run_in_threadpool(save_bin_profile, action, config.scope, config.sites)
    except (BinManageError, PersonalMemoryError, psycopg.Error):
        _LOGGER.warning("BinKeeper profile update failed")
        notice = "save-failed"
    return _redirect(config.base_path, bin_code, notice)


async def _photo_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    image = await _first_photo(form)
    notice = "photo-added"
    try:
        if image is None:
            raise BinManageError("a photo is required")
        action = PhotoAction(
            bin_code=bin_code,
            image=image,
            action_id=_form_text(form.get("action_id")),
            received_at=datetime.now(UTC),
        )
        await run_in_threadpool(add_bin_photo, action, config.scope)
    except (
        BinManageError,
        BinPhotoMediaError,
        PersonalMemoryError,
        BlobVaultError,
        OSError,
        psycopg.Error,
    ):
        _LOGGER.warning("BinKeeper photo addition failed")
        notice = "photo-failed"
    return _redirect(config.base_path, bin_code, notice)


async def _location_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    action = LocationAction(
        bin_code=bin_code,
        site=_form_text(form.get("current_site")),
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    notice = "location-saved"
    try:
        await run_in_threadpool(place_bin, action, config.scope, config.sites)
    except (BinManageError, BinInventoryError, psycopg.Error):
        _LOGGER.warning("BinKeeper location update failed")
        notice = "save-failed"
    return _redirect(config.base_path, bin_code, notice)


async def _containment_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    action = ContainmentAction(
        bin_code=bin_code,
        event_kind=_form_text(form.get("action")),
        container_code=_form_text(form.get("container_code")),
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    notice = f"containment-{action.event_kind}ed"
    try:
        await run_in_threadpool(change_bin_containment, action, config.scope)
    except (BinManageError, BinInventoryError, psycopg.Error):
        _LOGGER.warning("BinKeeper containment update failed")
        notice = "containment-failed"
    return _redirect(config.base_path, bin_code, notice)


async def _print_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    action = PrintAction(
        bin_code=bin_code,
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    try:
        outcome = await run_in_threadpool(reprint_bin, action, config.scope)
    except (BinManageError, PersonalMemoryError, BinLabelError, psycopg.Error):
        _LOGGER.warning("BinKeeper reprint failed")
        outcome = "failed"
    notice = {
        "sent": "label-sent",
        "replayed": "label-replayed",
        "failed": "print-failed",
        "unknown": "print-unknown",
    }[outcome]
    return _redirect(config.base_path, bin_code, notice)


async def _not_found_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    site = _form_text(form.get("site"))
    if not site.strip():
        return _redirect(config.base_path, bin_code, "retrieval-needs-site")
    action = RetrievalAction(
        bin_code=bin_code,
        site=site,
        outcome="not_found",
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    record = config.record_retrieval or record_retrieval_outcome
    try:
        await run_in_threadpool(record, action, config.scope)
    except (BinInventoryError, BinManageError, psycopg.Error):
        _LOGGER.warning("BinKeeper not-found record failed")
        return _redirect(config.base_path, bin_code, "retrieval-failed")
    return _triage_redirect(config.base_path, bin_code, "not-found-recorded")


def _triage_page(config: ManageRouteConfig, bin_code: str, notice: str) -> object:
    try:
        view = config.load_view(
            bin_code=bin_code,
            tenant_id=config.scope.tenant_id,
            corpus_id=config.scope.corpus_id,
        )
    except BinManageNotFoundError:
        raise HTTPException(status_code=404, detail="bin not found") from None
    except (psycopg.Error, ServingRoleUnavailableError):
        _LOGGER.warning("BinKeeper could not load a triage page")
        raise HTTPException(status_code=503, detail="bin details are unavailable") from None
    site = view["current_site"]
    load_triage = config.load_triage or load_triage_view
    candidates: list[TriageCandidate] = []
    if site:
        try:
            candidates = load_triage(
                bin_code=bin_code,
                site=site,
                tenant_id=config.scope.tenant_id,
                corpus_id=config.scope.corpus_id,
            )
        except (psycopg.Error, ServingRoleUnavailableError):
            _LOGGER.warning("BinKeeper could not load triage candidates")
            raise HTTPException(status_code=503, detail="triage is unavailable") from None
    code = quote(bin_code, safe="")
    return page_response(
        config.chrome.render(
            "bin_triage.html",
            bin_code=view["bin_code"],
            site=site,
            candidates=candidates,
            mark_action=surface_path(config.base_path, f"/manage/{code}/triage/mark"),
            mark_action_id=str(uuid4()),
            manage_url=surface_path(config.base_path, f"/manage/{code}"),
            notice=_manage_notice(notice),
            catalog_url="/bins/",
            photo_url=surface_path(config.base_path, "/"),
            register_url=surface_path(config.base_path, "/register"),
            stash_url=surface_path(config.base_path, "/stash"),
            binkeeper_section="catalog",
            surface_label="Where else?",
        )
    )


async def _triage_mark_response(
    request: Request,
    bin_code: str,
    config: ManageRouteConfig,
) -> RedirectResponse:
    form = await request.form()
    site = _form_text(form.get("site"))
    target = _form_text(form.get("target_bin")) or bin_code
    verdict = _form_text(form.get("verdict"))
    outcomes: dict[str, RetrievalOutcome] = {
        "seen": "confirm",
        "missing": "not_found",
        "found": "fetch",
    }
    outcome = outcomes.get(verdict)
    if outcome is None or not site.strip():
        return _triage_redirect(config.base_path, bin_code, "retrieval-failed")
    action = RetrievalAction(
        bin_code=bin_code if verdict == "found" else target,
        site=site,
        outcome=outcome,
        action_id=_form_text(form.get("action_id")),
        received_at=datetime.now(UTC),
    )
    record = config.record_retrieval or record_retrieval_outcome
    try:
        await run_in_threadpool(record, action, config.scope)
    except (BinInventoryError, BinManageError, psycopg.Error):
        _LOGGER.warning("BinKeeper triage mark failed")
        return _triage_redirect(config.base_path, bin_code, "retrieval-failed")
    if verdict == "found":
        return _redirect(config.base_path, bin_code, "found-recorded")
    notice = "candidate-confirmed" if verdict == "seen" else "candidate-missing"
    return _triage_redirect(config.base_path, bin_code, notice)


def record_retrieval_outcome(action: RetrievalAction, scope: BinActionScope) -> None:
    """Append one retrieval outcome to the trip ledger on the owner connection."""
    from binkeeper.bin_inventory import record_event
    from binkeeper.db import connect

    with connect() as conn:
        record_event(
            conn,
            event_kind=action.outcome,
            bin_code=action.bin_code,
            site=action.site,
            occurred_at=action.received_at,
            source_label="manage-web",
            idempotency_key=action.action_id or None,
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )


def load_triage_view(
    *, bin_code: str, site: str, tenant_id: str, corpus_id: str
) -> list[TriageCandidate]:
    """Rank the other bins believed at this site by expected regret. Read-only."""
    from binkeeper.bin_priority import reconfirm_priorities
    from binkeeper.db import connect

    with connect(role="serving") as conn:
        ranked = reconfirm_priorities(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    candidates: list[TriageCandidate] = [
        TriageCandidate(
            bin_code=candidate.bin_code,
            confidence_label=f"{round(candidate.confidence * 100)}%",
            stale=candidate.abstained,
        )
        for candidate in ranked
        if candidate.site == site and candidate.bin_code != bin_code
    ]
    return candidates[:12]


def load_manage_view(*, bin_code: str, tenant_id: str, corpus_id: str) -> ManageView:
    """Load the current owner-safe fields for one exact known bin."""
    from binkeeper.bin_inventory import bin_where
    from binkeeper.bin_nesting import load_bin_containment
    from binkeeper.bin_passport import bin_passport, load_known_bin_codes
    from binkeeper.db import connect

    code = bin_code.strip()
    with connect(role="serving") as conn:
        known_codes = load_known_bin_codes(conn, tenant_id=tenant_id, corpus_id=corpus_id)
        if code not in known_codes:
            raise BinManageNotFoundError(f"unknown bin {code!r}")
        passport = bin_passport(conn, code, tenant_id=tenant_id, corpus_id=corpus_id)
        graph = load_bin_containment(conn, tenant_id=tenant_id, corpus_id=corpus_id)
        container_options: tuple[str, ...] = ()
        if passport.container_code is None and passport.current_site is not None:
            container_options = tuple(
                candidate
                for candidate in known_codes
                if candidate != code
                and code not in graph.path_for(candidate)
                and bin_where(
                    conn,
                    candidate,
                    tenant_id=tenant_id,
                    corpus_id=corpus_id,
                ).site
                == passport.current_site
            )
        has_photo = _has_linked_photo(
            conn,
            bin_code=code,
            scope=BinActionScope(tenant_id=tenant_id, corpus_id=corpus_id),
        )
    return {
        "bin_code": passport.bin_code,
        "theme": passport.theme or "",
        "contents": passport.sibling_contents[-1] if passport.sibling_contents else "",
        "home_site": passport.home_site or "",
        "current_site": passport.current_site or "",
        "container_code": passport.container_code or "",
        "containment_path": passport.containment_path,
        "contained_bin_codes": passport.contained_bin_codes,
        "container_options": container_options,
        "catalog_photo_url": (
            f"/bins/photo/{quote(passport.bin_code, safe='')}" if has_photo else None
        ),
    }


def save_bin_profile(
    action: ProfileAction,
    scope: BinActionScope,
    valid_sites: tuple[str, ...],
) -> None:
    """Append one reviewed profile snapshot on the owner connection."""
    from binkeeper.bin_manage import update_bin_profile
    from binkeeper.db import connect

    if action.home_site and action.home_site not in valid_sites:
        raise BinManageError("home_site is not in the BinKeeper site list")
    update = BinProfileUpdate(
        bin_code=action.bin_code,
        theme=action.theme,
        contents=action.contents,
        home_site=action.home_site,
        action_id=action.action_id,
        observed_at=action.received_at,
    )
    with connect() as conn:
        update_bin_profile(conn, update, scope=scope)


def add_bin_photo(action: PhotoAction, scope: BinActionScope) -> None:
    """Validate and capture-link a photo before writing its local ciphertext."""
    from binkeeper.bin_manage import append_bin_photo
    from binkeeper.blob_vault import blob_store_from_config, put_blob, vault_key_from_config
    from binkeeper.db import connect

    validate_bin_photo(action.image)
    digest = hashlib.sha256(action.image).hexdigest()
    addition = BinPhotoAddition(
        bin_code=action.bin_code,
        photo_sha256=digest,
        action_id=action.action_id,
        observed_at=action.received_at,
    )
    with connect() as conn, conn.transaction():
        capture = append_bin_photo(conn, addition, scope=scope)
        if capture.already_existed:
            return
        store = blob_store_from_config()
        key, key_ref = vault_key_from_config()
        record = put_blob(
            conn,
            store,
            action.image,
            key=key,
            key_ref=key_ref,
            content_type="image",
            tenant_id=scope.tenant_id,
            corpus_id=scope.corpus_id,
        )
        if record.plaintext_sha256 != digest:
            raise BlobVaultError("stored photo digest differs from its capture link")
    try:  # advisory: a manage photo may also witness anchor co-locations
        from binkeeper.bin_colocation import harvest_photo_colocations

        # Its own connection, like the drop path's harvest: the capture and
        # ciphertext above are already committed, and this lane must not reuse
        # their closed connection (BINK-45) or sink the photo when it fails.
        with connect() as harvest_conn:
            harvest_photo_colocations(
                harvest_conn,
                action.image,
                evidence_ref=f"photo:{digest}",
                tenant_id=scope.tenant_id,
                corpus_id=scope.corpus_id,
            )
    except Exception:  # advisory lane: swallow, log, move on
        _LOGGER.warning("BinKeeper co-location harvest failed")


def place_bin(
    action: LocationAction,
    scope: BinActionScope,
    valid_sites: tuple[str, ...],
) -> None:
    """Append one reviewed placement event on the owner connection."""
    from binkeeper.bin_manage import place_existing_bin
    from binkeeper.db import connect

    if action.site not in valid_sites:
        raise BinManageError("site is not in the BinKeeper site list")
    placement = BinPlacement(
        bin_code=action.bin_code,
        site=action.site,
        action_id=action.action_id,
        occurred_at=action.received_at,
    )
    with connect() as conn:
        place_existing_bin(conn, placement, scope=scope)


def change_bin_containment(action: ContainmentAction, scope: BinActionScope) -> None:
    """Append one reviewed physical pack or unpack event."""
    from binkeeper.bin_nesting import (
        BinContainmentAppend,
        BinNestingError,
        record_bin_containment,
    )
    from binkeeper.db import connect

    try:
        with connect() as conn:
            record_bin_containment(
                conn,
                BinContainmentAppend(
                    event_kind=action.event_kind,
                    bin_code=action.bin_code,
                    container_code=action.container_code,
                    occurred_at=action.received_at,
                    source_label="binkeeper-manage",
                    idempotency_key=f"bincontainment:{action.action_id}",
                    tenant_id=scope.tenant_id,
                    corpus_id=scope.corpus_id,
                ),
            )
    except BinNestingError as exc:
        raise BinManageError(str(exc)) from exc


def reprint_bin(action: PrintAction, scope: BinActionScope) -> PrintOutcome:
    """Reserve and attempt one current saved label without replaying registration."""
    from binkeeper import bin_label
    from binkeeper.db import connect

    queue = bin_label.BIN_LABEL_CUPS_QUEUE.strip()
    if not queue:
        return "failed"
    with connect() as conn:
        label = _prepare_label(conn, action.bin_code, scope, queue)
        reservation = _reserve_print_intent(conn, action, scope, label)
    if reservation.already_existed:
        return "replayed"
    return _attempt_print(label)


def _prepare_label(
    conn: psycopg.Connection,
    bin_code: str,
    scope: BinActionScope,
    queue: str,
) -> PreparedLabel:
    """Render one label from the bin's current saved profile."""
    from binkeeper import bin_label
    from binkeeper.bin_passport import bin_passport, load_known_bin_codes

    code = bin_code.strip()
    if code not in load_known_bin_codes(
        conn,
        tenant_id=scope.tenant_id,
        corpus_id=scope.corpus_id,
    ):
        raise BinManageNotFoundError(f"unknown bin {code!r}")
    passport = bin_passport(
        conn,
        code,
        tenant_id=scope.tenant_id,
        corpus_id=scope.corpus_id,
    )
    contents = passport.sibling_contents[-1] if passport.sibling_contents else None
    return PreparedLabel(
        bin_code=code,
        queue=queue,
        tspl=bin_label.render_tspl(
            passport.bin_code,
            theme=passport.theme,
            site=passport.home_site,
            contents=contents,
        ),
    )


def _reserve_print_intent(
    conn: psycopg.Connection,
    action: PrintAction,
    scope: BinActionScope,
    label: PreparedLabel,
) -> CaptureResult:
    """Append the durable one-attempt boundary before printer I/O."""
    from binkeeper.bin_manage import reserve_label_print_intent

    intent = BinLabelPrintIntent(
        bin_code=label.bin_code,
        action_id=action.action_id,
        requested_at=action.received_at,
        queue=label.queue,
        tspl_sha256=hashlib.sha256(label.tspl.encode("utf-8")).hexdigest(),
    )
    return reserve_label_print_intent(conn, intent, scope=scope)


def _attempt_print(label: PreparedLabel) -> PrintOutcome:
    """Classify exactly one local CUPS handoff attempt."""
    from binkeeper import bin_label

    try:
        bin_label.send_to_printer(label.tspl, cups_queue=label.queue)
    except BinLabelError as exc:
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            return "unknown"
        return "failed"
    return "sent"


def _has_linked_photo(
    conn: psycopg.Connection,
    *,
    bin_code: str,
    scope: BinActionScope,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM captures
        WHERE tenant_id = %s
          AND corpus_id = %s
          AND raw_payload->'metadata'->>'kind' = 'bin_capture'
          AND raw_payload->'metadata'->>'bin_code' = %s
          AND NULLIF(btrim(COALESCE(
              raw_payload->'metadata'->'photo'->>'blob_ref',
              raw_payload->'metadata'->'photo'->>'sha256'
          )), '') IS NOT NULL
        LIMIT 1
        """,
        (scope.tenant_id, scope.corpus_id, bin_code),
    ).fetchone()
    return row is not None


async def _first_photo(form: FormData) -> bytes | None:
    for upload in form.getlist("photo"):
        if isinstance(upload, UploadFile):
            image = await upload.read()
            if image:
                return image
    return None


def _action_urls(base_path: str, bin_code: str) -> dict[str, str]:
    code = quote(bin_code, safe="")
    return {
        name: surface_path(base_path, f"/manage/{code}/{suffix}")
        for name, suffix in (
            ("profile_action", "profile"),
            ("location_action", "location"),
            ("containment_action", "containment"),
            ("photo_action", "photo"),
            ("print_action", "print"),
            ("not_found_action", "not-found"),
        )
    }


def _triage_redirect(base_path: str, bin_code: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, f"/manage/{quote(bin_code, safe='')}/triage")
    return RedirectResponse(f"{path}?notice={quote(notice, safe='')}", status_code=303)


def _redirect(base_path: str, bin_code: str, notice: str) -> RedirectResponse:
    path = surface_path(base_path, f"/manage/{quote(bin_code, safe='')}")
    location = f"{path}?notice={quote(notice, safe='')}"
    return RedirectResponse(location, status_code=303)


def _form_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _manage_notice(key: str) -> dict[str, str] | None:
    notices = {
        "profile-saved": ("ok", "Changes saved."),
        "location-saved": ("ok", "Current location updated."),
        "containment-packed": ("ok", "This bin is now inside the selected container."),
        "containment-unpacked": ("ok", "This bin was taken out of its container."),
        "containment-failed": (
            "error",
            "Nothing was changed. Check both bins and try again.",
        ),
        "photo-added": ("ok", "Photo added. The catalog now uses this newest photo."),
        "label-sent": ("ok", "One label request was sent to the local print queue."),
        "label-replayed": (
            "info",
            "That print request was already handled; no second attempt was made.",
        ),
        "save-failed": ("error", "Nothing was changed. Check the fields and try again."),
        "photo-failed": ("error", "The photo was not added. Choose it again and retry."),
        "print-failed": ("error", "The label could not be requested. Check the printer."),
        "print-unknown": (
            "error",
            "Print status is unknown. Check the printer before trying again.",
        ),
        "not-found-recorded": (
            "info",
            "Recorded that the bin was not where the map claimed. "
            "While you are searching, mark each bin you actually see.",
        ),
        "candidate-confirmed": ("ok", "Thanks — that sighting re-confirmed the bin's location."),
        "candidate-missing": ("info", "Recorded another miss. Its location confidence dropped."),
        "found-recorded": ("ok", "Recorded the successful retrieval. Location re-confirmed."),
        "retrieval-failed": ("error", "Nothing was recorded. Try again."),
        "retrieval-needs-site": (
            "error",
            "This bin has no claimed site to contradict; set its location first.",
        ),
    }
    found = notices.get(key)
    return {"kind": found[0], "message": found[1]} if found else None
