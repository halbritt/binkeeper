"""BinKeeper photo-drop surface (RFC 0088 T4 / RFC 0093 P5).

A tailnet-fronted write surface: the owner uploads photos of a bin's contents
plus free-text notes; the surface stores each photo in the A11 blob vault and
runs the local vision worker (Qwen3-VL on peecee) to propose a bin label. The
proposal is advisory -- it never captures a bin or mutates inventory on its own.
Reviewed registration and existing-bin profile/photo/location/print actions are
separate explicit routes in this isolated owner surface.

Follows the RFC 0081 chrome contract (HTML only through templates + chrome) and
the D124/D136 origin discipline (loopback bind, paired-origin gate, HTTPS
terminated by Tailscale Serve). Mounted by operator_web; served by the persistent
binkeeper-operator unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile

from binkeeper.bin_photo_web.manage import (
    ManageLoader,
    ManageRouteConfig,
    install_manage_routes,
    load_manage_view,
)
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
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                binkeeper_section="photo",
                sites=list(_SITE_PREFIXES),
                surface_label="Bin photo drop",
            )
        )

    @app.post("/")
    async def submit(request: Request, _origin: None = Depends(origin_check)) -> object:
        form_data = await request.form()
        notes = _clean(form_data.get("notes"))
        site = _clean(form_data.get("site"))
        requested_code = _clean(form_data.get("bin_code"))
        images: list[bytes] = []
        for upload in form_data.getlist("photos"):
            if isinstance(upload, UploadFile):
                data = await upload.read()
                if data:
                    images.append(data)
        view = await run_in_threadpool(
            _analyze, images=images, notes=notes, site=site, requested_code=requested_code
        )
        return page_response(
            chrome.render(
                "bin_photo_result.html",
                new_href=surface_path(normalized_base_path, "/"),
                confirm_action=surface_path(normalized_base_path, "/register/confirm"),
                align_action=surface_path(normalized_base_path, "/printer/align"),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
                binkeeper_section="photo",
                surface_label="Bin photo drop",
                **view,
            )
        )

    def _render_register(view: dict[str, object]) -> object:
        template = {
            "done": "bin_register_done.html",
            "pick": "bin_register_pick.html",
        }.get(str(view.get("mode")), "bin_register_form.html")
        return page_response(
            chrome.render(
                template,
                form_action=surface_path(normalized_base_path, "/register"),
                confirm_action=surface_path(normalized_base_path, "/register/confirm"),
                new_href=surface_path(normalized_base_path, "/register"),
                catalog_url="/bins/",
                photo_url=surface_path(normalized_base_path, "/"),
                register_url=surface_path(normalized_base_path, "/register"),
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
        view = await run_in_threadpool(
            _register_confirmed,
            bin_code=_clean(form_data.get("bin_code")),
            site=_clean(form_data.get("site")),
            theme=_clean(form_data.get("theme")),
            contents=_clean(form_data.get("contents")),
            lat=_float(form_data.get("lat")),
            lon=_float(form_data.get("lon")),
            accuracy_m=_float(form_data.get("accuracy_m")),
            photo_sha256=_clean(form_data.get("photo_sha256")),
            code_source=_clean(form_data.get("code_source")),
            label_count=label_count,
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
        ),
    )

    return app


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


def _analyze(
    *, images: list[bytes], notes: str | None, site: str | None, requested_code: str | None
) -> dict[str, object]:
    """Store the photos, then best-effort propose a label; build a view model.

    Storage is the guaranteed primary action and is reported on its own: the
    ORIGINAL full-resolution bytes are preserved in the vault regardless of
    whether the vision model is reachable. The label is a best-effort
    enrichment; a model outage yields ``label_error`` (calm, retryable), never
    a lost photo or a failed request.
    """
    if not images:
        return {
            "photo_count": 0,
            "blob_refs": [],
            "store_error": None,
            "proposal": None,
            "label_error": "No photos were uploaded.",
            "site": site,
            "suggested_bin_code": None,
            "all_sites": list(_SITE_PREFIXES),
            "proposed_contents": "",
        }
    blob_refs, store_error = _store_photos(images)

    proposal: dict[str, object] | None = None
    label_error: str | None = None
    from binkeeper.bin_vision import BinVisionError, OllamaVisionClient, propose_bin_label

    try:
        proposal = propose_bin_label(OllamaVisionClient(), images, notes=notes).to_json()
    except BinVisionError as exc:
        label_error = str(exc)

    accepts = proposal.get("accepts") if isinstance(proposal, dict) else None
    proposed_contents = (
        ", ".join(str(item) for item in accepts) if isinstance(accepts, list) else ""
    )
    return {
        "photo_count": len(images),
        "blob_refs": blob_refs,
        "store_error": store_error,
        "proposal": proposal,
        "label_error": label_error,
        "site": site,
        "suggested_bin_code": requested_code or _suggest_bin_code(site),
        "all_sites": list(_SITE_PREFIXES),
        "proposed_contents": proposed_contents,
    }


def _store_photos(images: list[bytes]) -> tuple[list[str], str | None]:
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
        with connect() as conn:
            for image in images:
                record = put_blob(
                    conn, store, image, key=key, key_ref=key_ref, content_type="image"
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
    from binkeeper.bin_vision import OllamaVisionClient, read_bin_code

    code = read_bin_code(OllamaVisionClient(), image)
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
) -> dict[str, object]:
    try:
        from binkeeper.bin_register import register_bin
        from binkeeper.db import connect

        with connect() as conn:
            result = register_bin(
                conn,
                bin_code=bin_code,
                site=site,
                gps=gps,  # type: ignore[arg-type]
                photo_sha256=photo_sha,
                contents_text=contents,
                theme=theme,
            )
    except Exception as exc:  # surface the failure; nothing partial is claimed
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
    if label_count:
        from binkeeper.bin_label import (
            BIN_LABEL_CUPS_QUEUE,
            BinLabelError,
            render_tspl,
            send_to_printer,
        )

        if result.already_existed:
            print_error = (
                "This registration already existed, so no label was sent to avoid a "
                "duplicate label."
            )
        elif not BIN_LABEL_CUPS_QUEUE:
            print_error = "No local CUPS queue is configured for BinKeeper labels."
        else:
            try:
                tspl = render_tspl(
                    result.bin_code,
                    theme=theme,
                    site=result.site,
                    contents=contents,
                    copies=label_count,
                )
                plan = send_to_printer(tspl, cups_queue=BIN_LABEL_CUPS_QUEUE)
                printed = True
                print_target = plan.target
            except BinLabelError as exc:
                print_error = str(exc)
    return {
        "mode": "done",
        "bin_code": result.bin_code,
        "code_source": code_source,
        "site": result.site,
        "already_existed": result.already_existed,
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
