"""Shared Origin and Sec-Fetch guard for local operator web surfaces.

This module also hosts the RFC 0076 legible-refusal layer:

- A **paired-origin allowlist** (``allowed_paired_origins``) so an opt-in
  tailnet front can grant exactly one ``(scheme, host, port)`` tuple without
  widening the independent scheme/host sets into their cross-product. The guard
  accepts an Origin iff it matches the existing independent sets **or** an exact
  ``(scheme, host)`` paired entry (both still require the bound port + a root
  path).
- A single ``RefusalTriple`` renderer (a stable ``X-BinKeeper-Refusal`` header, a
  JSON body, and an inert HTML page) whose verbosity is **loopback-only**: a
  request arriving via a Tailscale Serve front (detected by the injected
  ``Tailscale-User-*`` header) gets a terse refusal, never the configuration
  remedy. The **raw received Origin is never reflected** into any sink — only
  the server-computed missed-field and (loopback-only) the expected set render.
"""

from __future__ import annotations

import html
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, NoReturn, Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

DEFAULT_ALLOWED_ORIGIN_HOSTS: tuple[str, ...] = ("127.0.0.1", "localhost")
DEFAULT_ALLOWED_SCHEMES: tuple[str, ...] = ("http",)
DEFAULT_ALLOWED_SEC_FETCH_SITES: tuple[str, ...] = ("same-origin",)

#: Header carrying the machine-readable refusal rule (RFC 0076 Part 3). The
#: value is always a server-computed enum/grammar; the raw Origin never reaches
#: it, so it cannot be used for header injection.
REFUSAL_HEADER = "X-BinKeeper-Refusal"

#: Tailscale Serve injects identity headers (e.g. ``Tailscale-User-Login``) on
#: every request it proxies; a direct loopback request never carries one. This
#: is the trustworthy front-vs-loopback signal (RFC 0076 §Privacy, OQ7): the
#: loopback bind is reachable from the tailnet only via Serve, so the header's
#: presence — not the spoofable Origin/Host or the always-loopback peer
#: address — decides whether the refusal renders its full remedy.
_TAILSCALE_IDENTITY_HEADER_PREFIX = "tailscale-user-"

#: A single ``(scheme, host)`` paired allow-entry. Ports are matched against the
#: bound port, so the effective grant is one ``(scheme, host, port)`` tuple.
PairedOrigin = tuple[str, str]

MissedField = Literal["scheme", "host", "port", "no_match", "sec_fetch_site", "missing"]

_ORIGIN_RULE = "origin_mismatch"


class RequestLike(Protocol):
    """Minimal request shape needed by the shared Origin guard."""

    @property
    def headers(self) -> Mapping[str, str]:
        """HTTP headers, case-insensitive for Starlette/FastAPI requests."""
        ...


def _normalize_host(host: str) -> str:
    """Lowercase and strip a trailing dot, matching browser Origin hosts."""
    return host.lower().rstrip(".")


# Any of these marks a request relayed by a front/proxy: Tailscale Serve adds
# X-Forwarded-* server-side, and a remote client cannot remove headers the
# proxy itself stamps.
FORWARDED_REQUEST_HEADERS: tuple[str, ...] = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
)
DIRECT_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})


def is_direct_loopback_request(request: RequestLike) -> bool:
    """True iff the request reached us directly on a loopback Host.

    Keys on request provenance, not deployment configuration (owner
    feedback 2026-06-07: a surface withheld whenever a front *exists* is
    useless on a host that always runs one). A request relayed by any
    front carries ``FORWARDED_REQUEST_HEADERS``; a direct local request
    carries none and targets a loopback Host.
    """
    if any(header in request.headers for header in FORWARDED_REQUEST_HEADERS):
        return False
    host_header = request.headers.get("host", "")
    hostname = _normalize_host(urlsplit(f"//{host_header}").hostname or "")
    return hostname in DIRECT_LOOPBACK_HOSTS


def matches_paired_front(
    request: RequestLike,
    *,
    allowed_paired_origins: tuple[tuple[str, str], ...],
    bound_port: int,
) -> bool:
    """True iff the request is addressed to an owner-paired front origin (D136).

    The grant is the same one ``(scheme, host, port)`` tuple the Origin guard
    accepts (D136): the Host header's hostname must equal a paired host, its port
    the bound port, and — when the front stamps ``X-Forwarded-Proto`` (Tailscale
    Serve does, server-side) — the proto the paired scheme. The bind stays
    loopback-only, so a request carrying the paired Host came through the owner's
    Serve front or a local process; both are inside the declared tailnet trust
    boundary. Consolidated from per-panel copies (deep-refactoring item #7).
    """
    if not allowed_paired_origins:
        return False
    split = urlsplit(f"//{request.headers.get('host', '')}")
    hostname = (split.hostname or "").lower().rstrip(".")
    try:
        port = split.port
    except ValueError:
        return False
    if not hostname or port != bound_port:
        return False
    forwarded_proto = request.headers.get("x-forwarded-proto")
    return any(
        hostname == paired_host.lower().rstrip(".")
        and (forwarded_proto is None or forwarded_proto == paired_scheme)
        for paired_scheme, paired_host in allowed_paired_origins
    )


def expected_origin_patterns(
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_ORIGIN_HOSTS,
    bound_port: int | None = None,
    allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES,
    allowed_paired_origins: tuple[PairedOrigin, ...] = (),
) -> tuple[str, ...]:
    """Return user-facing expected Origin patterns.

    Renders one entry per independent ``(scheme, host)`` pair plus one per
    paired allow-entry, so the loopback 403 hint matches the set actually
    accepted by ``require_origin``. Defaults stay ``http`` only; ``https`` only
    appears via an explicit scheme opt-in or a paired entry (``--front
    tailnet``).
    """
    port = str(bound_port) if bound_port is not None else "<bound-port>"
    patterns = [f"{scheme}://{host}:{port}" for scheme in allowed_schemes for host in allowed_hosts]
    patterns += [f"{scheme}://{host}:{port}" for scheme, host in allowed_paired_origins]
    return tuple(dict.fromkeys(patterns))


def request_host_port(request: RequestLike) -> int | None:
    """Return the numeric port from the request Host header."""
    host_header = request.headers.get("host", "")
    if not host_header:
        return None
    try:
        return urlsplit(f"//{host_header}").port
    except ValueError:
        return None


def is_via_front(request: RequestLike) -> bool:
    """Return whether the request arrived via a Tailscale Serve front.

    Keys on the presence of any ``Tailscale-User-*`` header that Serve injects
    for proxied requests. A direct loopback request carries none. This is the
    binding loopback-vs-front signal for refusal verbosity (RFC 0076 §Privacy).
    """
    for name in request.headers:
        if name.lower().startswith(_TAILSCALE_IDENTITY_HEADER_PREFIX):
            return True
    return False


def classify_origin_miss(
    observed: str | None,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_ORIGIN_HOSTS,
    allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES,
    allowed_paired_origins: tuple[PairedOrigin, ...] = (),
    bound_port: int | None = None,
) -> MissedField:
    """Return which field of ``observed`` missed the accept set.

    Side-effect free and computed purely from server state and the parsed
    ``observed`` Origin. **Never returns the raw ``observed`` string** — only a
    fixed field label — so it is safe to render into any sink. A ``(scheme,
    host)`` combination that is individually familiar but not an accepted pair
    (the cross-product) classifies as ``no_match``.
    """
    if not observed:
        return "missing"
    try:
        parsed = urlsplit(observed)
    except ValueError:
        return "no_match"

    scheme = parsed.scheme
    host = _normalize_host(parsed.hostname or "")
    normalized_hosts = tuple(_normalize_host(h) for h in allowed_hosts)
    paired = tuple((s, _normalize_host(h)) for s, h in allowed_paired_origins)

    combo_ok = (scheme in allowed_schemes and host in normalized_hosts) or (
        (scheme, host) in paired
    )
    if combo_ok:
        # scheme + host are jointly acceptable, so the miss is the port (or a
        # non-root path). ``port`` is the closest single-field label.
        return "port"

    scheme_seen = scheme in allowed_schemes or any(s == scheme for s, _ in paired)
    host_seen = host in normalized_hosts or any(h == host for _, h in paired)
    if not scheme_seen and not host_seen:
        return "no_match"
    if not scheme_seen:
        return "scheme"
    if not host_seen:
        return "host"
    return "no_match"


def require_origin(
    request: RequestLike,
    *,
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_ORIGIN_HOSTS,
    bound_port: int | None = None,
    allowed_schemes: tuple[str, ...] = DEFAULT_ALLOWED_SCHEMES,
    require_sec_fetch_site: bool = True,
    allowed_sec_fetch_sites: tuple[str, ...] = DEFAULT_ALLOWED_SEC_FETCH_SITES,
    allowed_paired_origins: tuple[PairedOrigin, ...] = (),
) -> None:
    """Enforce a loopback-style Origin and Sec-Fetch-Site policy.

    Accepts an Origin iff it matches the independent ``allowed_schemes`` and
    ``allowed_hosts`` sets **or** an exact ``allowed_paired_origins`` entry --
    in both cases requiring the bound port and a root path. The paired set is
    additive and never admits the cross-product of the independent sets.
    """
    expected = expected_origin_patterns(
        allowed_hosts=allowed_hosts,
        bound_port=bound_port,
        allowed_schemes=allowed_schemes,
        allowed_paired_origins=allowed_paired_origins,
    )
    origin = request.headers.get("origin")
    target_port = bound_port if bound_port is not None else request_host_port(request)
    if origin is None:
        _raise_origin_refusal(missed_field="missing", expected=expected)

    try:
        parsed_origin = urlsplit(origin)
        origin_port = parsed_origin.port
    except ValueError as exc:
        # A malformed netloc (e.g. a CR/LF-mangled or non-numeric port) is a
        # miss, never an accept; the raw Origin is still never reflected.
        raise _origin_refusal(missed_field="no_match", expected=expected) from exc

    normalized_hosts = tuple(_normalize_host(h) for h in allowed_hosts)
    paired = tuple((s, _normalize_host(h)) for s, h in allowed_paired_origins)
    origin_scheme = parsed_origin.scheme
    origin_host = _normalize_host(parsed_origin.hostname or "")

    combo_ok = (origin_scheme in allowed_schemes and origin_host in normalized_hosts) or (
        (origin_scheme, origin_host) in paired
    )
    path_ok = parsed_origin.path in ("", "/")
    port_ok = target_port is not None and origin_port == target_port

    if not (combo_ok and path_ok and port_ok):
        missed = classify_origin_miss(
            origin,
            allowed_hosts=allowed_hosts,
            allowed_schemes=allowed_schemes,
            allowed_paired_origins=allowed_paired_origins,
            bound_port=target_port,
        )
        _raise_origin_refusal(missed_field=missed, expected=expected)

    sec_fetch_site = request.headers.get("sec-fetch-site")
    if (
        require_sec_fetch_site
        and sec_fetch_site is not None
        and sec_fetch_site not in allowed_sec_fetch_sites
    ):
        _raise_origin_refusal(
            missed_field="sec_fetch_site",
            expected=tuple(f"sec-fetch-site={site}" for site in allowed_sec_fetch_sites),
        )


# ---------------------------------------------------------------------------
# Legible refusal rendering (RFC 0076 Part 3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RefusalTriple:
    """One refusal rendered three ways from a single source of truth.

    Carries only server-computed values: the ``rule``, the ``missed_field``
    label, the expected-pattern set, and a fixed copy-paste ``remedy``. The raw
    received Origin is intentionally absent, so no attacker-controlled input can
    reach the header, the JSON body, or the HTML page.
    """

    rule: str
    missed_field: str | None
    expected: tuple[str, ...]
    remedy: str

    def header_value(self, *, verbose: bool) -> str:
        """Return the ``X-BinKeeper-Refusal`` value (terse over the front)."""
        if verbose and self.missed_field:
            return f"{self.rule}; field={self.missed_field}"
        return self.rule

    def json_body(self, *, verbose: bool) -> dict[str, object]:
        """Return a back-compatible refusal body.

        Carries both a top-level ``error`` and a mirrored ``detail`` object so
        consumers that read either shape keep working. Loopback requests also
        get ``missed_field``, ``expected``, and ``remedy``; via-front requests
        get only the rule.
        """
        inner: dict[str, object] = {"error": self.rule}
        if verbose:
            if self.missed_field:
                inner["missed_field"] = self.missed_field
            if self.expected:
                inner["expected"] = list(self.expected)
            if self.remedy:
                inner["remedy"] = self.remedy
        body: dict[str, object] = dict(inner)
        body["detail"] = dict(inner)
        return body

    def html_page(self, *, verbose: bool) -> str:
        """Return an inert HTML refusal page (no script, no auto-refresh)."""
        rule = html.escape(self.rule)
        if not verbose:
            return (
                '<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>Request refused</title></head><body>"
                "<h1>Request refused</h1>"
                f"<p>Refusal: <code>{rule}</code></p>"
                "</body></html>"
            )
        field = html.escape(self.missed_field or "")
        remedy = html.escape(self.remedy)
        expected_items = "".join(
            f"<li><code>{html.escape(pattern)}</code></li>" for pattern in self.expected
        )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            "<title>Request refused</title></head><body>"
            "<h1>Request refused</h1>"
            f"<p>Refusal: <code>{rule}</code>"
            + (f" (missed field: <code>{field}</code>)" if field else "")
            + "</p>"
            + (f"<p>{remedy}</p>" if remedy else "")
            + (f"<ul>{expected_items}</ul>" if expected_items else "")
            + "</body></html>"
        )


class OriginRefusal(HTTPException):
    """A refusal carrying a :class:`RefusalTriple` for legible rendering.

    Subclasses ``HTTPException`` so a dedicated handler can win over a generic
    ``HTTPException`` handler. ``detail`` is the **terse** body (code review N1):
    the loopback-only verbose detail (remedy + ``expected``, which under a paired
    front includes the tailnet MagicDNS host) is rendered **only** by the installed
    handler via :func:`render_refusal`. So if a surface ever raises this without
    ``install_origin_refusal_handler``, the default handler fails **safe** (terse)
    rather than leaking the remedy/host to a via-front peer.
    """

    def __init__(self, *, status_code: int, triple: RefusalTriple) -> None:
        self.triple = triple
        super().__init__(status_code=status_code, detail=triple.json_body(verbose=False))


def _remedy_for(missed_field: MissedField, expected: tuple[str, ...]) -> str:
    """Return a fixed, server-authored remedy hint (loopback-only at render)."""
    if missed_field == "scheme":
        return (
            "Origin scheme not accepted; set "
            "BINKEEPER_OPERATOR_ALLOWED_ORIGIN_SCHEMES=http,https or run "
            "'binkeeper operator serve --front tailnet' to allowlist the https front."
        )
    if missed_field == "host":
        return (
            "Origin host not accepted; add it to BINKEEPER_OPERATOR_ALLOWED_ORIGINS or run "
            "'binkeeper operator serve --front tailnet' to auto-allowlist this machine's "
            "MagicDNS name."
        )
    if missed_field == "port":
        return "Origin port did not match the bound port; expected: " + ", ".join(expected)
    if missed_field == "sec_fetch_site":
        return "Send Sec-Fetch-Site: same-origin from a same-origin browser context."
    if missed_field == "missing":
        return "Send an Origin header matching one of: " + ", ".join(expected)
    return "Use one of the accepted origins: " + ", ".join(expected)


def _origin_refusal(*, missed_field: MissedField, expected: tuple[str, ...]) -> OriginRefusal:
    triple = RefusalTriple(
        rule=_ORIGIN_RULE,
        missed_field=missed_field,
        expected=tuple(expected),
        remedy=_remedy_for(missed_field, expected),
    )
    return OriginRefusal(status_code=403, triple=triple)


def _raise_origin_refusal(*, missed_field: MissedField, expected: tuple[str, ...]) -> NoReturn:
    raise _origin_refusal(missed_field=missed_field, expected=expected)


def render_refusal(request: Request, triple: RefusalTriple, *, status_code: int) -> Response:
    """Render a :class:`RefusalTriple` as JSON or inert HTML.

    Verbosity is loopback-only (see :func:`is_via_front`); content negotiation
    keys on ``Accept: text/html``. Always sets the ``X-BinKeeper-Refusal`` header.
    Shared by the Origin 403 handler and the cross-surface 409 gate stub so the
    two never disagree about the rule, the missed field, or the remedy.
    """
    verbose = not is_via_front(request)
    headers = {REFUSAL_HEADER: triple.header_value(verbose=verbose)}
    accept = request.headers.get("accept", "")
    if "text/html" in accept.lower():
        return HTMLResponse(
            triple.html_page(verbose=verbose),
            status_code=status_code,
            headers=headers,
        )
    return JSONResponse(
        triple.json_body(verbose=verbose),
        status_code=status_code,
        headers=headers,
    )


def render_refusal_response(request: Request, exc: OriginRefusal) -> Response:
    """Render an :class:`OriginRefusal` (delegates to :func:`render_refusal`)."""
    return render_refusal(request, exc.triple, status_code=exc.status_code)


def install_origin_refusal_handler(app: FastAPI) -> None:
    """Register the loopback-only :class:`OriginRefusal` renderer on ``app``.

    Scoped to ``OriginRefusal`` so other 403s (e.g. capture's session guard)
    keep their existing envelopes. Idempotent enough for repeated mounting:
    re-registering the same handler is harmless.
    """

    async def _handler(request: Request, exc: Exception) -> Response:
        assert isinstance(exc, OriginRefusal)
        return render_refusal_response(request, exc)

    app.add_exception_handler(OriginRefusal, _handler)
