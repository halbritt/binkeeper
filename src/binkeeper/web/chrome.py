"""Shared local-first chrome for operator web surfaces (RFC 0081).

This module is the single home of the shared Jinja render path: surface
chrome construction, the shared static mount, and the only sanctioned
``HTMLResponse`` constructors outside the ratchet exemption list. Chrome
performs no origin/tier/auth gating — gates run in handlers/dependencies
before context construction (RFC 0081 § Decision 3).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from starlette.templating import Jinja2Templates

LOCAL_ONLY_HELP_COPY: str = (
    "BinKeeper runs entirely on your machine. No cloud service. No telemetry. "
    "No CDN. The browser fetches assets from this process only."
)

PHASE4_FUTURE_COPY: str = "Phase 4 work is not yet built. Tracked in RFC 0021 / D044 / D069 / D079."

AUDIT_EGRESS_STATUS: str = "no network egress"

SHARED_STATIC_MOUNT_PATH: str = "/shared-static"
SHARED_STATIC_MOUNT_NAME: str = "shared-static"


def audit_footer_copy(bind_address: str, *, egress_status: str = AUDIT_EGRESS_STATUS) -> str:
    """Render the local-only audit footer sentence."""
    return f"local-only · loopback bind: {bind_address} · {egress_status}."


def asset_version() -> str:
    """Return the binkeeper package version used for static cache busting."""
    try:
        return metadata.version("binkeeper")
    except metadata.PackageNotFoundError:
        return "0"


def shared_static_urls(base_path: str | None = None) -> dict[str, str]:
    """Versioned, mount-aware URLs for the shared shell assets.

    Every shell page needs these in its template context; the shell template
    carries no defaults so a missing or wrong-prefix URL fails loudly rather
    than silently resolving against the wrong mount.
    """
    from binkeeper.web.paths import static_path

    version = asset_version()
    return {
        "binkeeper_css_url": static_path(base_path, "binkeeper.css", version=version),
        "keyboard_static_url": static_path(base_path, "keyboard.js", version=version),
    }


def mount_shared_static(app: FastAPI) -> None:
    """Mount the shared static directory on ``app`` once, idempotently.

    Serves with ``Cache-Control: no-cache`` (ETag revalidation) so stylesheet
    freshness does not depend on the package version changing; the ``?v=``
    query of :func:`shared_static_urls` is the release-boundary
    belt-and-suspenders, not the sole mechanism.
    """
    from binkeeper.web import assets as shared_assets

    if any(getattr(route, "name", None) == SHARED_STATIC_MOUNT_NAME for route in app.routes):
        return
    app.mount(
        SHARED_STATIC_MOUNT_PATH,
        _NoCacheStaticFiles(directory=str(shared_assets.static_dir())),
        name=SHARED_STATIC_MOUNT_NAME,
    )


@dataclass(frozen=True)
class SurfaceChrome:
    """Configured rendering chrome for one operator surface."""

    surface: str
    templates: Jinja2Templates
    base_context: dict[str, str] = field(default_factory=dict)

    def context(self, **values: object) -> dict[str, object]:
        """Merge the chrome base context with per-request values."""
        merged: dict[str, object] = dict(self.base_context)
        merged.update(values)
        return merged

    def render(self, template_name: str, /, **values: object) -> str:
        """Render a template through the chrome environment and base context."""
        return self.templates.env.get_template(template_name).render(self.context(**values))


def build_surface_chrome(
    surface_name: str,
    *,
    base_path: str | None = None,
    extra_template_dir: Path | str | None = None,
) -> SurfaceChrome:
    """Build the full rendering chrome for a surface in one call.

    Invoked at surface-module initialization, never per-request. Returns a
    Jinja environment (surface templates first, shared templates second) with
    autoescape pinned on, plus the base context every shell page needs. The
    shared static mount is separate (:func:`mount_shared_static`) because
    FastAPI mounts register on the app, not in template state.
    """
    import jinja2
    from starlette.templating import Jinja2Templates

    from binkeeper.web import assets as shared_assets

    loaders: list[jinja2.BaseLoader] = []
    if extra_template_dir is not None:
        loaders.append(jinja2.FileSystemLoader(str(extra_template_dir)))
    loaders.append(jinja2.FileSystemLoader(str(shared_assets.template_dir())))
    env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(loaders),
        autoescape=True,
    )
    templates = Jinja2Templates(env=env)
    base_context = {"surface": surface_name, **shared_static_urls(base_path)}
    return SurfaceChrome(surface=surface_name, templates=templates, base_context=base_context)


def fragment_response(parts: Sequence[str]) -> HTMLResponse:
    """Join template-rendered fragments into one ``text/html`` response.

    A sanctioned ``HTMLResponse`` constructor for surface modules (RFC 0081
    scan predicate (b)); callers must pass fragments produced by the Jinja
    render path, never hand-assembled markup.
    """
    from fastapi.responses import HTMLResponse

    return HTMLResponse("\n".join(parts))


def page_response(document: str, *, status_code: int = 200) -> HTMLResponse:
    """Wrap a chrome-rendered document in a ``text/html`` response.

    A sanctioned ``HTMLResponse`` constructor for surface modules (RFC 0081
    scan predicate (b)); callers must pass :meth:`SurfaceChrome.render`
    output, never hand-assembled markup. ``status_code`` carries the error
    status for shell-rendered error pages (the shared HTML error handlers).
    """
    from fastapi.responses import HTMLResponse

    return HTMLResponse(document, status_code=status_code)


class _NoCacheStaticFiles:
    """StaticFiles wrapper forcing ``Cache-Control: no-cache`` revalidation."""

    def __init__(self, *, directory: str) -> None:
        from starlette.staticfiles import StaticFiles

        self._static = StaticFiles(directory=directory)

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        async def send_with_no_cache(message: object) -> None:
            if isinstance(message, dict) and message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [
                    (name, value) for name, value in headers if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-cache"))
                message = {**message, "headers": headers}
            await send(message)  # type: ignore[operator]  # ASGI send callable

        await self._static(scope, receive, send_with_no_cache)  # type: ignore[arg-type]  # ASGI passthrough
