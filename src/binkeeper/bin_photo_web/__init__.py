"""BinKeeper photo-drop surface (RFC 0088 T4 / RFC 0093 P5).

A tailnet-fronted write surface: the owner uploads photos of a bin's contents
plus free-text notes; the surface stores each photo in the A11 blob vault and
runs the configured vision provider to propose a bin label. The
proposal is advisory -- it never captures a bin or mutates inventory on its own.
Reviewed registration and existing-bin profile/photo/location/print actions are
separate explicit routes in this isolated owner surface.

Follows the RFC 0081 chrome contract (HTML only through templates + chrome) and
the D124/D136 origin discipline (loopback bind, paired-origin gate, HTTPS
terminated by Tailscale Serve). Served by the standalone BinKeeper service.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import perf_counter
from typing import Final, cast
from urllib.parse import quote
from uuid import UUID, uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from PIL import Image
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from binkeeper.bin_catalog_web import require_bin_catalog_request
from binkeeper.bin_create import BinCreationConflict, BinCreationError, CreateBinRequest, create_bin
from binkeeper.bin_label import BinLabelError
from binkeeper.bin_manage import DEFAULT_BIN_ACTION_SCOPE, BinActionScope, BinManageError
from binkeeper.bin_photo_media import (
    MAX_PHOTO_BYTES,
    BinPhotoMediaError,
    thumbnail_jpeg,
    validate_bin_photos,
)
from binkeeper.bin_photo_web.manage import (
    LabelDriftDismissalRecorder,
    ManageLoader,
    ManageRouteConfig,
    RetrievalRecorder,
    TriageLoader,
    install_manage_routes,
    load_manage_view,
)
from binkeeper.bin_photo_web.stash import (
    QuorumFounder,
    QuorumLoader,
    StashDecisionRecorder,
    StashDeckLoader,
    StashRouteConfig,
    StashRunCreator,
    StashRunLister,
    WavePlanLoader,
    WaveStopCompleter,
    install_stash_routes,
)
from binkeeper.bin_quick_label import QuickLabelError, QuickLabelRequest, create_quick_label
from binkeeper.bin_saved_photos import (
    MAX_LABEL_PHOTOS,
    PAGE_SIZE,
    ConfiguredSavedPhotos,
    SavedPhotoSelectionError,
    SavedPhotoSource,
    SavedPhotosUnavailableError,
    attach_photos_to_bin,
    selected_photo_ids,
)
from binkeeper.interactive_vision import INTERACTIVE_VISION_BUDGET, TimedVisionClient, VisionBusy
from binkeeper.sites import SITE_PREFIXES
from binkeeper.web.chrome import build_surface_chrome, mount_shared_static, page_response
from binkeeper.web.origin import install_origin_refusal_handler, require_origin
from binkeeper.web.paths import normalize_base_path, surface_path

_TEMPLATE_DIR: Final[Path] = Path(__file__).parent / "templates"
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
BIN_PHOTO_WEB_ENABLED_ENV: Final[str] = "BINKEEPER_BIN_PHOTO_WEB_ENABLED"

# D170 canonical sites and their bin-code prefixes (single source: binkeeper.sites).
_SITE_PREFIXES: Final[dict[str, str]] = SITE_PREFIXES


def create_app(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    base_path: str = "",
    allowed_origin_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "testserver"),
    allowed_origin_schemes: tuple[str, ...] = ("http",),
    allowed_paired_origins: tuple[tuple[str, str], ...] = (),
    tenant_id: str = "personal",
    corpus_id: str = "personal",
    manage_loader: ManageLoader | None = None,
    triage_loader: TriageLoader | None = None,
    retrieval_recorder: RetrievalRecorder | None = None,
    label_drift_dismissal_recorder: LabelDriftDismissalRecorder | None = None,
    stash_deck_loader: StashDeckLoader | None = None,
    stash_run_lister: StashRunLister | None = None,
    stash_run_creator: StashRunCreator | None = None,
    stash_decision_recorder: StashDecisionRecorder | None = None,
    wave_plan_loader: WavePlanLoader | None = None,
    wave_stop_completer: WaveStopCompleter | None = None,
    quorum_loader: QuorumLoader | None = None,
    quorum_founder: QuorumFounder | None = None,
    saved_photo_source: SavedPhotoSource | None = None,
) -> FastAPI:
    """Build the loopback-bound bin-photo-drop app."""
    if host.strip() not in _LOOPBACK_HOSTS:
        raise ValueError(f"bin-photo web refuses non-loopback host: {host}")
    normalized_base_path = normalize_base_path(base_path)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.port = int(port)
    app.state.allowed_origin_hosts = tuple(allowed_origin_hosts)
    app.state.allowed_origin_schemes = tuple(allowed_origin_schemes)
    app.state.allowed_paired_origins = tuple(allowed_paired_origins)
    app.state.tenant_id = tenant_id
    app.state.corpus_id = corpus_id
    install_origin_refusal_handler(app)
    mount_shared_static(app)
    chrome = build_surface_chrome(
        "bin_photo", base_path=normalized_base_path, extra_template_dir=_TEMPLATE_DIR
    )
    load_manage = manage_loader or load_manage_view
    saved_photos = saved_photo_source or ConfiguredSavedPhotos(
        tenant_id=tenant_id, corpus_id=corpus_id
    )

    def _enforce_origin(request: Request) -> None:
        require_origin(
            request,
            allowed_hosts=app.state.allowed_origin_hosts,
            allowed_schemes=app.state.allowed_origin_schemes,
            allowed_paired_origins=app.state.allowed_paired_origins,
            bound_port=app.state.port,
            require_sec_fetch_site=False,
        )

    def origin_check(request: Request) -> None:
        if request.headers.get("origin") is not None:
            _enforce_origin(request)

    def strict_origin_check(request: Request) -> None:
        """Require an explicit browser origin for reviewed registration/print."""
        _enforce_origin(request)

    @app.get("/")
    def form() -> object:
        return page_response(
            chrome.render(
                "bin_photo_form.html",
                form_action=surface_path(normalized_base_path, "/"),
                register_href=surface_path(normalized_base_path, "/register"),
                quick_label_url=surface_path(normalized_base_path, "/quick-label"),
                saved_photos_url=surface_path(normalized_base_path, "/saved-photos"),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                stash_url=surface_path(normalized_base_path, "/stash"),
                binkeeper_section="photo",
                sites=list(_SITE_PREFIXES),
                surface_label="Bin photo drop",
            )
        )

    def _render_quick_label(view: dict[str, object]) -> object:
        done = view.get("mode") == "done"
        code = str(view.get("bin_code") or "")
        manage_url = surface_path(normalized_base_path, f"/manage/{quote(code, safe='')}")
        return page_response(
            chrome.render(
                "bin_register_done.html" if done else "bin_quick_label.html",
                form_action=surface_path(normalized_base_path, "/quick-label"),
                new_href=surface_path(normalized_base_path, "/quick-label"),
                add_photo_url=f"{manage_url}#photos",
                reprint_url=f"{manage_url}#label",
                bin_details_url=manage_url,
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                stash_url=surface_path(normalized_base_path, "/stash"),
                binkeeper_section="photo",
                surface_label="Quick label",
                sites=list(_SITE_PREFIXES),
                b1_available=_b1_available(),
                quick_label=True,
                **{"action_id": str(uuid4()), **view},
            )
        )

    @app.get("/quick-label")
    def quick_label_form() -> object:
        return _render_quick_label({"mode": "form"})

    @app.post("/quick-label")
    async def quick_label_submit(
        request: Request,
        _origin: None = Depends(strict_origin_check),
    ) -> object:
        form_data = await request.form()
        action_id = _clean(form_data.get("action_id")) or ""
        site = _clean(form_data.get("site")) or ""
        theme = _clean(form_data.get("theme")) or ""
        contents = _clean(form_data.get("contents")) or ""
        printer = _clean(form_data.get("printer")) or "cups"
        copies = 2 if _clean(form_data.get("label_count")) == "2" else 1
        try:
            result = await run_in_threadpool(
                create_quick_label,
                QuickLabelRequest(site, theme, contents, printer, copies, action_id),
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
        except (QuickLabelError, BinLabelError) as exc:
            return _render_quick_label(
                {
                    "mode": "form",
                    "error": str(exc),
                    "site": site,
                    "theme": theme,
                    "contents": contents,
                    "printer": printer,
                    "label_count": copies,
                    "action_id": action_id or str(uuid4()),
                }
            )
        except psycopg.Error:
            return _render_quick_label(
                {
                    "mode": "form",
                    "error": "Could not save the bin. Try again.",
                    "site": site,
                    "theme": theme,
                    "contents": contents,
                    "printer": printer,
                    "label_count": copies,
                    "action_id": action_id,
                }
            )
        return _render_quick_label(
            {
                "mode": "done",
                "bin_code": result.bin_code,
                "site": result.site,
                "already_existed": result.already_existed,
                "has_gps": False,
                "photo_stored": False,
                "print_requested": True,
                "label_count": copies,
                "printed": result.printed,
                "print_target": result.print_target,
                "print_error": result.print_error,
                "print_unknown": result.print_unknown,
                "code_source": "assigned automatically",
            }
        )

    @app.post("/")
    async def submit(request: Request, _origin: None = Depends(origin_check)) -> object:
        form_data = await request.form()
        notes = _clean(form_data.get("notes"))
        site = _clean(form_data.get("site"))
        requested_code = _clean(form_data.get("bin_code"))
        input_error = None
        try:
            images = await _uploaded_photos(form_data)
        except BinPhotoMediaError as exc:
            images, input_error = [], str(exc)
        view = await run_in_threadpool(
            _analyze,
            images=images,
            notes=notes,
            site=site,
            requested_code=requested_code,
            theme=_clean(form_data.get("theme")),
            contents=_clean(form_data.get("contents")),
            input_error=input_error,
        )
        response = _render_photo_result(view)
        if images and not view.get("admission_error"):
            response.background = BackgroundTask(_harvest_colocations, images)
        return response

    def _render_photo_result(view: dict[str, object]) -> Response:
        from binkeeper.bin_b1 import B1_ADDRESS
        from binkeeper.bin_label import BIN_LABEL_CUPS_QUEUE

        started = perf_counter()
        response = page_response(
            chrome.render(
                "bin_photo_result.html",
                new_href=surface_path(normalized_base_path, "/"),
                saved_photos_url=surface_path(normalized_base_path, "/saved-photos"),
                confirm_action=surface_path(normalized_base_path, "/register/confirm"),
                align_action=surface_path(normalized_base_path, "/printer/align"),
                can_align_label=bool(BIN_LABEL_CUPS_QUEUE.strip()),
                b1_available=bool(B1_ADDRESS),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                stash_url=surface_path(normalized_base_path, "/stash"),
                binkeeper_section="photo",
                surface_label="Bin photo drop",
                **{"action_id": str(uuid4()), **view},
            )
        )
        timings = view.get("phase_timings")
        if isinstance(timings, dict):
            timings["render_ms"] = round((perf_counter() - started) * 1000, 3)
            logging.getLogger(__name__).info("BinKeeper photo phases %s", json.dumps(timings))
        response.status_code = int(cast(int, view.get("status_code", 200)))
        return response

    def saved_photo_access(request: Request) -> None:
        require_bin_catalog_request(
            request, allowed_paired_origins=allowed_paired_origins, bound_port=port
        )

    def _render_saved_photos(
        *,
        offset: int = 0,
        error: str | None = None,
        status_code: int = 200,
        selected: list[str] | None = None,
        notes: str = "",
        site: str = "",
        bin_code: str = "",
        theme: str = "",
        contents: str = "",
        target_bin_code: str = "",
        notice: str | None = None,
    ) -> Response:
        try:
            photos = saved_photos.list_photos(offset=offset, limit=PAGE_SIZE + 1)
            bins = saved_photos.list_bins()
        except SavedPhotosUnavailableError:
            photos = []
            bins = []
            error = "Could not load saved photos. Try again."
            status_code = 503
        saved_url = surface_path(normalized_base_path, "/saved-photos")
        response = page_response(
            chrome.render(
                "bin_saved_photos.html",
                photos=photos[:PAGE_SIZE],
                selected=selected or [],
                error=error,
                notes=notes,
                site=site,
                bin_code=bin_code,
                theme=theme,
                contents=contents,
                bins=bins,
                target_bin_code=target_bin_code,
                notice=notice,
                sites=list(_SITE_PREFIXES),
                max_photos=MAX_LABEL_PHOTOS,
                offset=offset,
                previous_url=f"{saved_url}?offset={max(0, offset - PAGE_SIZE)}" if offset else None,
                next_url=f"{saved_url}?offset={offset + PAGE_SIZE}"
                if len(photos) > PAGE_SIZE
                else None,
                saved_photos_url=saved_url,
                manage_base_url=surface_path(normalized_base_path, "/manage"),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                stash_url=surface_path(normalized_base_path, "/stash"),
                binkeeper_section="saved-photos",
                surface_label="Saved photos",
            )
        )
        response.status_code = status_code
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.get("/saved-photos", dependencies=[Depends(saved_photo_access)])
    def saved_photo_list(offset: int = Query(default=0, ge=0)) -> Response:
        return _render_saved_photos(offset=offset)

    @app.post(
        "/saved-photos/upload",
        dependencies=[Depends(saved_photo_access), Depends(strict_origin_check)],
    )
    async def saved_photo_upload(request: Request) -> Response:
        error: str | None = None
        images: list[bytes] = []
        try:
            images = await _uploaded_photos(await request.form())
            await run_in_threadpool(validate_bin_photos, images)
        except BinPhotoMediaError as exc:
            error = str(exc)
        if error:
            return await run_in_threadpool(_render_saved_photos, error=error, status_code=400)
        _, store_error = await run_in_threadpool(
            _store_photos,
            images,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
        if store_error:
            return await run_in_threadpool(
                _render_saved_photos,
                error="Could not save the photos. Please retry.",
                status_code=503,
            )
        return RedirectResponse(
            surface_path(normalized_base_path, "/saved-photos"), status_code=303
        )

    @app.get("/saved-photos/{photo_id}/thumbnail", dependencies=[Depends(saved_photo_access)])
    def saved_photo_thumbnail(photo_id: str) -> Response:
        headers = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        try:
            photo = saved_photos.load_photos(selected_photo_ids([photo_id]))[0]
            thumbnail = thumbnail_jpeg(photo.image)
        except (
            SavedPhotoSelectionError,
            SavedPhotosUnavailableError,
            Image.DecompressionBombError,
            OSError,
            SyntaxError,
            ValueError,
        ):
            raise HTTPException(404, "Saved photo is unavailable", headers=headers) from None
        return Response(thumbnail, media_type="image/jpeg", headers=headers)

    @app.post(
        "/saved-photos/attach",
        dependencies=[Depends(saved_photo_access), Depends(strict_origin_check)],
    )
    async def saved_photo_attach(
        request: Request, offset: int = Query(default=0, ge=0)
    ) -> Response:
        form_data = await request.form()
        selection = [str(value) for value in form_data.getlist("photo_ids")]
        code = _clean(form_data.get("target_bin_code")) or ""
        error: str | None = None
        status_code = 200
        try:
            if not code:
                raise SavedPhotoSelectionError("Choose an existing bin.")
            photos = await run_in_threadpool(
                saved_photos.load_photos, selected_photo_ids(selection)
            )
            await run_in_threadpool(
                attach_photos_to_bin,
                photos,
                code,
                tenant_id=tenant_id,
                corpus_id=corpus_id,
            )
        except (SavedPhotoSelectionError, BinManageError, BinPhotoMediaError) as exc:
            error, status_code = str(exc), 400
        except (SavedPhotosUnavailableError, psycopg.Error):
            error, status_code = "Could not attach the photos. Please retry.", 503
        return await run_in_threadpool(
            _render_saved_photos,
            offset=offset,
            error=error,
            status_code=status_code,
            selected=selection if error else [],
            target_bin_code=code,
            theme=_clean(form_data.get("theme")) or "",
            contents=_clean(form_data.get("contents")) or "",
            notes=_clean(form_data.get("notes")) or "",
            site=_clean(form_data.get("site")) or "",
            bin_code=_clean(form_data.get("bin_code")) or "",
            notice=None if error else f"Selected photos are attached to {code}.",
        )

    @app.post(
        "/saved-photos", dependencies=[Depends(saved_photo_access), Depends(strict_origin_check)]
    )
    async def saved_photo_retry(request: Request, offset: int = Query(default=0, ge=0)) -> Response:
        form_data = await request.form()
        selection = [str(value) for value in form_data.getlist("photo_ids")]
        notes = _clean(form_data.get("notes")) or ""
        site = _clean(form_data.get("site")) or ""
        bin_code = _clean(form_data.get("bin_code")) or ""
        try:
            selection = selected_photo_ids(selection)
            photos = await run_in_threadpool(saved_photos.load_photos, selection)
        except (SavedPhotoSelectionError, SavedPhotosUnavailableError) as exc:
            return await run_in_threadpool(
                _render_saved_photos,
                offset=offset,
                error=(
                    "Could not read the selected photos. Try again."
                    if isinstance(exc, SavedPhotosUnavailableError)
                    else str(exc)
                ),
                status_code=503 if isinstance(exc, SavedPhotosUnavailableError) else 400,
                selected=selection,
                notes=notes,
                site=site,
                bin_code=bin_code,
                theme=_clean(form_data.get("theme")) or "",
                contents=_clean(form_data.get("contents")) or "",
            )
        view = await run_in_threadpool(
            _analyze,
            images=[photo.image for photo in photos],
            stored_blob_refs=[photo.sha256 for photo in photos],
            theme=_clean(form_data.get("theme")),
            contents=_clean(form_data.get("contents")),
            notes=notes or None,
            site=site or None,
            requested_code=bin_code or None,
        )
        # Reusing an old photo is not a new location observation; do not harvest it again.
        return _render_photo_result(view)

    def _render_register(view: dict[str, object]) -> Response:
        template = {
            "done": "bin_register_done.html",
            "pick": "bin_register_pick.html",
        }.get(str(view.get("mode")), "bin_register_form.html")
        code = str(view.get("bin_code") or "")
        return page_response(
            chrome.render(
                template,
                form_action=surface_path(normalized_base_path, "/register"),
                confirm_action=surface_path(normalized_base_path, "/register/confirm"),
                new_href=surface_path(normalized_base_path, "/register"),
                add_photo_url=surface_path(
                    normalized_base_path, f"/manage/{quote(code, safe='')}#photos"
                ),
                bin_details_url=surface_path(
                    normalized_base_path, f"/manage/{quote(code, safe='')}"
                ),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                stash_url=surface_path(normalized_base_path, "/stash"),
                binkeeper_section="register",
                surface_label="Register bin",
                **view,
            )
        )

    @app.get("/register")
    def register_form(code: str = "") -> object:
        return _render_register({"mode": "form", "bin_code": code})

    @app.post("/register")
    async def register_submit(request: Request, _origin: None = Depends(origin_check)) -> object:
        form_data = await request.form()
        image = await _first_photo(form_data)
        view = await run_in_threadpool(
            _register_flow,
            image=image,
            bin_code=_clean(form_data.get("bin_code")),
            contents=_clean(form_data.get("contents")),
            geo_lat=_float(form_data.get("geo_lat")),
            geo_lon=_float(form_data.get("geo_lon")),
            geo_accuracy=_float(form_data.get("geo_accuracy")),
        )
        return _render_register(view)

    @app.post("/register/confirm")
    async def register_confirm(
        request: Request,
        _origin: None = Depends(strict_origin_check),
    ) -> object:
        form_data = await request.form()
        print_requested = _clean(form_data.get("print_label")) == "1"
        label_count = (
            2
            if print_requested and _clean(form_data.get("label_count")) == "2"
            else int(print_requested)
        )
        operation = _clean(form_data.get("operation"))
        if operation is None and not any(
            field in form_data
            for field in ("theme", "print_label", "action_id", "additional_photo_sha256")
        ):
            # Old physical-label forms also need one explicit reconfirmation;
            # preserve their GPS context without treating them as new-bin creation.
            response = _render_register(
                {
                    "mode": "pick",
                    "bin_code": _clean(form_data.get("bin_code")) or "",
                    "contents": _clean(form_data.get("contents")) or "",
                    "photo_sha256": _clean(form_data.get("photo_sha256")) or "",
                    "code_source": _clean(form_data.get("code_source")) or "entered",
                    "lat": _float(form_data.get("lat")),
                    "lon": _float(form_data.get("lon")),
                    "accuracy_m": _float(form_data.get("accuracy_m")),
                    "has_gps": _float(form_data.get("lat")) is not None,
                    "reason": "please confirm the site again after the application update",
                    "candidates": [_clean(form_data.get("site"))] if form_data.get("site") else [],
                    "all_sites": list(_SITE_PREFIXES),
                }
            )
            response.status_code = 409
            return response
        if operation != "observe":
            refs = tuple(
                str(ref)
                for ref in [
                    form_data.get("photo_sha256"),
                    *form_data.getlist("additional_photo_sha256"),
                ]
                if ref
            )
            action_id = _clean(form_data.get("action_id")) or ""
            creation_request = CreateBinRequest(
                action_id=action_id,
                bin_code=_clean(form_data.get("bin_code")) or "",
                site=_clean(form_data.get("site")) or "",
                theme=_clean(form_data.get("theme")) or "",
                contents=_clean(form_data.get("contents")) or "",
                photo_refs=refs,
                printer=_clean(form_data.get("printer")) or "cups",
                copies=label_count,
            )
            try:
                UUID(action_id)
                stale = operation != "create"
            except ValueError:
                stale = True
            new_review = _clean(form_data.get("new_action")) == "1"
            if stale or new_review:
                view = {
                    "error": "Review the saved details below, then confirm to create a new bin.",
                    "action_id": str(uuid4()),
                }
            else:
                view = await run_in_threadpool(
                    _register_and_view,
                    bin_code=creation_request.bin_code,
                    site=creation_request.site,
                    gps=None,
                    photo_sha=refs[0] if refs else None,
                    theme=creation_request.theme,
                    contents=creation_request.contents,
                    code_source=_clean(form_data.get("code_source")) or "owner review",
                    label_count=label_count,
                    printer=creation_request.printer,
                    creation=creation_request,
                    scope=BinActionScope(tenant_id, corpus_id),
                )
                if view.get("mode") == "done":
                    return _render_register(view)
            response = _render_photo_result(
                {
                    "photo_count": len(refs),
                    "blob_refs": refs,
                    "all_sites": list(_SITE_PREFIXES),
                    "site": creation_request.site,
                    "suggested_bin_code": creation_request.bin_code,
                    "proposed_theme": creation_request.theme,
                    "proposed_contents": creation_request.contents,
                    "theme_provided": bool(creation_request.theme),
                    "contents_provided": bool(creation_request.contents),
                    "action_id": action_id,
                    "printer": creation_request.printer,
                    "label_count": 2 if _clean(form_data.get("label_count")) == "2" else 1,
                    "creation_review": True,
                    **view,
                }
            )
            response.status_code = 200 if new_review else cast(int, view.get("status_code", 409))
            return response
        view = await run_in_threadpool(
            _register_confirmed,
            bin_code=_clean(form_data.get("bin_code")),
            site=_clean(form_data.get("site")),
            theme=None,
            contents=_clean(form_data.get("contents")),
            lat=_float(form_data.get("lat")),
            lon=_float(form_data.get("lon")),
            accuracy_m=_float(form_data.get("accuracy_m")),
            photo_sha256=_clean(form_data.get("photo_sha256")),
            code_source=_clean(form_data.get("code_source")),
        )
        return _render_register(view)

    @app.post("/printer/align")
    async def align_label(_origin: None = Depends(strict_origin_check)) -> object:
        from binkeeper.bin_label import BinLabelError

        try:
            target = await run_in_threadpool(_align_label)
        except BinLabelError as exc:
            import subprocess

            if isinstance(exc.__cause__, subprocess.TimeoutExpired):
                return JSONResponse(
                    {
                        "status": "unknown",
                        "detail": (
                            "Alignment status is unknown. Check the label position before "
                            "trying again."
                        ),
                    },
                    status_code=504,
                )
            return JSONResponse(
                {"status": "unavailable", "detail": str(exc)},
                status_code=503,
            )
        return {
            "status": "aligned",
            "detail": "Label advanced to the next boundary.",
            "target": target,
        }

    from binkeeper.bin_manage import BinActionScope

    install_manage_routes(
        app,
        ManageRouteConfig(
            base_path=normalized_base_path,
            chrome=chrome,
            scope=BinActionScope(tenant_id=tenant_id, corpus_id=corpus_id),
            sites=tuple(_SITE_PREFIXES),
            load_view=load_manage,
            strict_origin=strict_origin_check,
            load_triage=triage_loader,
            record_retrieval=retrieval_recorder,
            record_label_drift_dismissal=label_drift_dismissal_recorder,
        ),
    )

    install_stash_routes(
        app,
        StashRouteConfig(
            base_path=normalized_base_path,
            chrome=chrome,
            scope=BinActionScope(tenant_id=tenant_id, corpus_id=corpus_id),
            sites=tuple(_SITE_PREFIXES),
            strict_origin=strict_origin_check,
            load_deck=stash_deck_loader,
            list_runs=stash_run_lister,
            create_run=stash_run_creator,
            record_decision=stash_decision_recorder,
            load_wave=wave_plan_loader,
            complete_stop=wave_stop_completer,
            load_quorum=quorum_loader,
            found_bin=quorum_founder,
        ),
    )

    return app


def _harvest_colocations(images: list[bytes]) -> None:
    """Advisory: append witnessed anchor co-location rows from dropped photos.

    The drop flow must never fail because this lane did — any error degrades
    to a logged warning and an empty harvest.
    """
    import logging

    try:
        from binkeeper.bin_colocation import harvest_photo_colocations
        from binkeeper.db import connect

        with connect() as conn:
            for image in images:
                harvest_photo_colocations(conn, image)
    except Exception:  # advisory lane: swallow, log, move on
        logging.getLogger(__name__).warning("BinKeeper co-location harvest failed")


def _align_label() -> str:
    from binkeeper import bin_label

    queue = bin_label.BIN_LABEL_CUPS_QUEUE.strip()
    if not queue:
        raise bin_label.BinLabelError("No local CUPS queue is configured for BinKeeper labels.")
    plan = bin_label.send_to_printer(bin_label.render_formfeed_tspl(), cups_queue=queue)
    return plan.target


async def _first_photo(form_data: FormData) -> bytes | None:
    for upload in form_data.getlist("photo"):
        if isinstance(upload, UploadFile):
            data = await upload.read()
            if data:
                return data
    return None


async def _uploaded_photos(form_data: FormData) -> list[bytes]:
    uploads = [item for item in form_data.getlist("photos") if isinstance(item, UploadFile)]
    try:
        if not 1 <= len(uploads) <= MAX_LABEL_PHOTOS:
            raise BinPhotoMediaError(f"Choose between 1 and {MAX_LABEL_PHOTOS} photos of one bin.")
        images = []
        for upload in uploads:
            data = await upload.read(MAX_PHOTO_BYTES + 1)
            if not data or len(data) > MAX_PHOTO_BYTES:
                raise BinPhotoMediaError("Each photo must be nonempty and at most 20 MiB.")
            images.append(data)
        return images
    finally:
        for upload in uploads:
            await upload.close()


def _analyze(
    *,
    images: list[bytes],
    notes: str | None,
    site: str | None,
    requested_code: str | None,
    stored_blob_refs: list[str] | None = None,
    theme: str | None = None,
    contents: str | None = None,
    input_error: str | None = None,
) -> dict[str, object]:
    """Validate, preserve accepted originals, then attempt bounded advisory work."""
    timings: dict[str, object] = {
        "request_id": str(uuid4()),
        "photo_count": len(images),
        "validation_ms": 0.0,
        "storage_ms": 0.0,
        "model_ms": 0.0,
        "merge_overhead_ms": 0.0,
        "outcome": "manual",
    }
    started = perf_counter()
    admission_error = input_error
    if not admission_error:
        try:
            validate_bin_photos(images)
        except BinPhotoMediaError as exc:
            admission_error = str(exc)
    timings["validation_ms"] = round((perf_counter() - started) * 1000, 3)
    started = perf_counter()
    blob_refs, store_error = (
        (stored_blob_refs, None)
        if stored_blob_refs is not None
        else ([], None)
        if admission_error
        else _store_photos(images)
    )
    timings["storage_ms"] = round((perf_counter() - started) * 1000, 3)
    proposal: dict[str, object] | None = None
    label_error = admission_error
    status_code = 400 if admission_error else 200
    from binkeeper.bin_vision import (
        BinVisionError,
        GeminiVisionClient,
        OpenRouterVisionClient,
        default_vision_client,
        propose_bin_label,
    )

    if admission_error:
        timings["outcome"] = "refused"
    elif not (theme and contents):
        try:
            client = default_vision_client()
            workers = (
                min(len(images), 3)
                if isinstance(client, OpenRouterVisionClient | GeminiVisionClient)
                else 1
            )
            with INTERACTIVE_VISION_BUDGET.admit(workers):
                measured = TimedVisionClient(client)
                started = perf_counter()
                try:
                    proposal = propose_bin_label(
                        measured,
                        images,
                        notes=notes,
                        max_workers=workers,
                        fail_on_error=True,
                        contents_only=bool(theme),
                        existing_theme=theme,
                        theme_only=bool(contents),
                    ).to_json()
                finally:
                    elapsed = perf_counter() - started
                    timings["model_ms"] = round(measured.active_seconds * 1000, 3)
                    timings["merge_overhead_ms"] = round(
                        max(0, elapsed - measured.active_seconds) * 1000, 3
                    )
            timings["outcome"] = "completed"
        except BinVisionError as exc:
            label_error = str(exc)
            status_code = 429 if isinstance(exc, VisionBusy) else 200
            timings["outcome"] = "busy" if isinstance(exc, VisionBusy) else "model_failed"
            logging.getLogger(__name__).warning("BinKeeper label analysis failed: %s", exc)

    accepts = proposal.get("accepts") if isinstance(proposal, dict) else None
    proposed_contents = contents or (
        ", ".join(str(item) for item in accepts) if isinstance(accepts, list) else ""
    )
    return {
        "photo_count": len(images),
        "blob_refs": blob_refs,
        "store_error": store_error,
        "proposal": proposal,
        "label_error": label_error,
        "admission_error": admission_error,
        "status_code": status_code,
        "phase_timings": timings,
        "review_recovery": bool(admission_error),
        "site": site,
        "suggested_bin_code": requested_code or _suggest_bin_code(site),
        "all_sites": list(_SITE_PREFIXES),
        "proposed_contents": proposed_contents,
        "proposed_theme": theme or (proposal.get("theme", "") if proposal else ""),
        "theme_provided": bool(theme),
        "contents_provided": bool(contents),
        "analysis_skipped": bool(theme and contents) and not admission_error,
    }


def _store_photos(
    images: list[bytes],
    *,
    tenant_id: str = "personal",
    corpus_id: str = "personal",
) -> tuple[list[str], str | None]:
    """Store each ORIGINAL photo in the vault; return ``(refs, error)``.

    Storage failure is surfaced honestly (not swallowed) so the owner knows
    whether the photos were preserved -- that is the whole point of the drop.
    """
    try:
        from binkeeper.blob_vault import blob_store_from_config, put_blob, vault_key_from_config
        from binkeeper.db import connect

        store = blob_store_from_config()
        key, key_ref = vault_key_from_config()
        refs: list[str] = []
        with connect() as conn, conn.transaction():
            for image in images:
                record = put_blob(
                    conn,
                    store,
                    image,
                    key=key,
                    key_ref=key_ref,
                    content_type="image",
                    tenant_id=tenant_id,
                    corpus_id=corpus_id,
                )
                refs.append(record.plaintext_sha256)
        return refs, None
    except Exception as exc:  # report the failure; the label step still runs
        return [], f"{type(exc).__name__}: {exc}"


def _suggest_bin_code(site: str | None) -> str | None:
    prefix = _SITE_PREFIXES.get(site or "")
    if prefix is None:
        return None
    try:
        from binkeeper.bin_passport import load_known_bin_codes
        from binkeeper.db import connect

        with connect(role="serving") as conn:
            codes = load_known_bin_codes(conn)
    except Exception:  # suggestion is advisory; fall back to the first code
        return f"{prefix}-001"
    highest = 0
    for code in codes:
        if code.startswith(f"{prefix}-"):
            suffix = code[len(prefix) + 1 :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
    return f"{prefix}-{highest + 1:03d}"


def _register_flow(
    *,
    image: bytes | None,
    bin_code: str | None,
    contents: str | None,
    geo_lat: float | None = None,
    geo_lon: float | None = None,
    geo_accuracy: float | None = None,
) -> dict[str, object]:
    """Read the bin code + GPS from the photo, then register -- or ask what's missing.

    The zero-typing path: if the owner left the code blank, read it from the
    label's QR (exact), falling back to vision OCR of the printed code (a blurry
    shot). Only if both fail do we ask for the code. Location prefers the device
    geolocation the browser sends (iOS strips EXIF GPS from in-browser camera
    captures), falling back to the photo's own EXIF.
    """
    code, code_source = _detect_bin_code(image=image, entered=bin_code)
    if not code:
        return {
            "mode": "form",
            "bin_code": "",
            "contents": contents or "",
            "error": "I couldn't read a bin code from the photo. Retake with the label's "
            "QR in frame, or type the code below.",
        }
    from binkeeper.bin_geo import GpsFix, extract_gps
    from binkeeper.bin_harvest import load_sites, resolve_site

    photo_sha: str | None = None
    if image:
        refs, _err = _store_photos([image])
        photo_sha = refs[0] if refs else None
    if geo_lat is not None and geo_lon is not None:
        gps = GpsFix(lat=geo_lat, lon=geo_lon, accuracy_m=geo_accuracy)
    else:
        gps = extract_gps(image) if image else None
    resolution = None
    if gps is not None:
        resolution = resolve_site(gps.lat, gps.lon, load_sites(), accuracy_m=gps.accuracy_m)
    if resolution is not None and resolution.resolved and resolution.site:
        return _register_and_view(
            bin_code=code,
            site=resolution.site,
            gps=gps,
            photo_sha=photo_sha,
            contents=contents,
            code_source=code_source,
        )
    return {
        "mode": "pick",
        "bin_code": code,
        "code_source": code_source,
        "contents": contents or "",
        "candidates": list(resolution.candidates) if resolution is not None else [],
        "all_sites": list(_SITE_PREFIXES),
        "reason": _pick_reason(gps, resolution),
        "photo_sha256": photo_sha or "",
        "lat": gps.lat if gps else "",
        "lon": gps.lon if gps else "",
        "accuracy_m": gps.accuracy_m if (gps and gps.accuracy_m is not None) else "",
        "has_gps": gps is not None,
    }


def _detect_bin_code(*, image: bytes | None, entered: str | None) -> tuple[str | None, str]:
    """Resolve the bin code: an entered value wins, else QR, else vision OCR."""
    if entered:
        from binkeeper.bin_geo import normalize_bin_code

        return (normalize_bin_code(entered) or entered.strip(), "entered")
    if not image:
        return (None, "")
    from binkeeper.bin_geo import decode_bin_code

    code = decode_bin_code(image)
    if code:
        return (code, "the label QR")
    from binkeeper.bin_vision import BinVisionError, default_vision_client, read_bin_code

    try:
        with INTERACTIVE_VISION_BUDGET.admit(1):
            code = read_bin_code(default_vision_client(), image)
    except BinVisionError:
        return (None, "")
    if code:
        return (code, "the printed label")
    return (None, "")


def _register_confirmed(
    *,
    bin_code: str | None,
    site: str | None,
    theme: str | None,
    contents: str | None,
    lat: float | None,
    lon: float | None,
    accuracy_m: float | None,
    photo_sha256: str | None,
    code_source: str | None = None,
    label_count: int = 0,
    printer: str = "cups",
    additional_photo_sha256s: tuple[str, ...] = (),
) -> dict[str, object]:
    """Finish a registration after the owner picked the site; establish its anchor."""
    if not bin_code or not site:
        return {
            "mode": "form",
            "bin_code": bin_code or "",
            "contents": contents or "",
            "error": "Both a bin code and a site are required.",
        }
    import contextlib

    from binkeeper.bin_geo import GpsFix, upsert_site_anchor

    if lat is not None and lon is not None:
        gps = GpsFix(lat=lat, lon=lon, accuracy_m=accuracy_m)
    else:
        gps = None
    if gps is not None:
        # anchor persistence is best-effort; the bin still registers without it
        with contextlib.suppress(Exception):
            upsert_site_anchor(site, gps.lat, gps.lon)  # first pick at a site establishes it
    return _register_and_view(
        bin_code=bin_code,
        site=site,
        gps=gps,
        photo_sha=photo_sha256 or None,
        contents=contents,
        code_source=code_source or "",
        theme=theme,
        label_count=label_count,
        printer=printer,
        additional_photo_refs=additional_photo_sha256s,
    )


def _register_and_view(
    *,
    bin_code: str,
    site: str,
    gps: object | None,
    photo_sha: str | None,
    contents: str | None,
    code_source: str = "",
    theme: str | None = None,
    label_count: int = 0,
    printer: str = "cups",
    additional_photo_refs: tuple[str, ...] = (),
    creation: CreateBinRequest | None = None,
    scope: BinActionScope = DEFAULT_BIN_ACTION_SCOPE,
) -> dict[str, object]:
    try:
        from binkeeper.bin_manage import BinManageError, append_saved_bin_photos
        from binkeeper.bin_register import register_bin
        from binkeeper.db import connect

        if len(additional_photo_refs) >= MAX_LABEL_PHOTOS:
            raise BinManageError(f"Choose at most {MAX_LABEL_PHOTOS} photos.")
        with connect() as conn, conn.transaction():
            result = (
                create_bin(conn, creation, scope=scope)
                if creation is not None
                else register_bin(
                    conn,
                    bin_code=bin_code,
                    site=site,
                    gps=gps,  # type: ignore[arg-type]
                    photo_sha256=photo_sha,
                    contents_text=contents,
                    theme=theme,
                )
            )
            if additional_photo_refs:
                append_saved_bin_photos(
                    conn,
                    bin_code,
                    ((photo_sha,) if photo_sha else ()) + additional_photo_refs,
                )
    except Exception as exc:  # surface the failure; nothing partial is claimed
        if creation is not None:
            message = (
                str(exc)
                if isinstance(exc, BinCreationError | BinManageError)
                else "Could not save the bin. Your reviewed details are below; try again."
            )
            logging.getLogger(__name__).warning("Bin creation failed: %s", exc)
            return {
                "error": message,
                "creation_conflict": isinstance(exc, BinCreationConflict),
                "status_code": 409 if isinstance(exc, BinCreationError | BinManageError) else 503,
            }
        return {
            "mode": "form",
            "bin_code": bin_code,
            "contents": contents or "",
            "error": f"Could not register: {type(exc).__name__}: {exc}",
        }
    lat = getattr(gps, "lat", None)
    lon = getattr(gps, "lon", None)
    accuracy = getattr(gps, "accuracy_m", None)
    printed = False
    print_target = ""
    print_error: str | None = None
    print_unknown = False
    if label_count:
        from binkeeper.bin_label import BinLabelError, make_label_job
        from binkeeper.bin_print import submit_label

        if result.already_existed:
            print_error = (
                "This registration already existed, so no label was sent to avoid a "
                "duplicate label."
            )
        else:
            try:
                if creation is None:
                    raise BinLabelError("Printing requires a reviewed creation request.")
                job = make_label_job(
                    result.bin_code,
                    printer=printer,
                    theme=theme,
                    site=result.site,
                    contents=contents,
                    copies=label_count,
                )
                print_result = submit_label(result.bin_code, creation.action_id, job, scope=scope)
                printed = print_result.status == "submitted"
                print_target = print_result.display_target
                print_error = print_result.error
                print_unknown = print_result.status in {"unknown", "unresolved"}
            except BinLabelError as exc:
                print_error = str(exc)
    return {
        "mode": "done",
        "bin_code": result.bin_code,
        "code_source": code_source,
        "site": result.site,
        "already_existed": result.already_existed,
        "creation_replay": creation is not None and result.already_existed,
        "has_gps": gps is not None,
        "lat": lat if lat is not None else "",
        "lon": lon if lon is not None else "",
        "accuracy_m": accuracy if accuracy is not None else "",
        "photo_stored": bool(photo_sha),
        "print_requested": label_count > 0,
        "label_count": label_count,
        "printed": printed,
        "print_target": print_target,
        "print_error": print_error,
        "print_unknown": print_unknown,
    }


def _pick_reason(gps: object | None, resolution: object | None) -> str:
    if gps is None:
        return "the photo has no GPS, so pick the site once"
    if getattr(resolution, "too_loose", False):
        return "the GPS fix was too imprecise to place automatically"
    if getattr(resolution, "ambiguous", False):
        return "the GPS sits inside more than one site's range -- pick which one"
    return "no site anchor is near this spot yet -- your pick establishes it"


def _float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _b1_available() -> bool:
    from binkeeper.bin_b1 import B1_ADDRESS

    return bool(B1_ADDRESS)
