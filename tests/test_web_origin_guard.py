"""Unit tests for the shared Origin guard and RFC 0076 legible refusals."""

from __future__ import annotations

import pytest
from starlette.requests import Request

from binkeeper.web.origin import (
    REFUSAL_HEADER,
    OriginRefusal,
    RefusalTriple,
    classify_origin_miss,
    expected_origin_patterns,
    is_via_front,
    matches_paired_front,
    render_refusal_response,
    require_origin,
)

_PORT = 8765
_TAILNET_HOST = "host.example-tailnet.ts.net"


def _request(headers: dict[str, str]) -> Request:
    """Build a real Starlette request from raw headers (CR/LF preserved)."""
    raw = [
        (key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in headers.items()
    ]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw})


def _origin_headers(origin: str, *, host: str = f"127.0.0.1:{_PORT}") -> dict[str, str]:
    return {"host": host, "origin": origin, "sec-fetch-site": "same-origin"}


# --- paired-origin allowlist (Part 1, Acceptance #4) ------------------------


@pytest.mark.parametrize(
    "origin",
    [
        f"http://127.0.0.1:{_PORT}",
        f"https://{_TAILNET_HOST}:{_PORT}",
    ],
)
def test_paired_origin_accepts_loopback_http_and_paired_https(origin: str) -> None:
    require_origin(
        _request(_origin_headers(origin, host=f"127.0.0.1:{_PORT}")),
        bound_port=_PORT,
        allowed_paired_origins=(("https", _TAILNET_HOST),),
    )


@pytest.mark.parametrize(
    "origin",
    [
        f"https://127.0.0.1:{_PORT}",  # cross-product: paired scheme, loopback host
        f"http://{_TAILNET_HOST}:{_PORT}",  # cross-product: loopback scheme, paired host
    ],
)
def test_paired_origin_rejects_the_cross_product(origin: str) -> None:
    with pytest.raises(OriginRefusal) as excinfo:
        require_origin(
            _request(_origin_headers(origin)),
            bound_port=_PORT,
            allowed_paired_origins=(("https", _TAILNET_HOST),),
        )
    assert excinfo.value.status_code == 403


def test_paired_origin_without_explicit_port_misses_on_port() -> None:
    """A paired entry grants exactly one ``(scheme, host, port)`` tuple (D124).

    An Origin that omits the port (the browser default-port form, e.g. a
    Serve map on 443) is refused with missed field ``port`` — intended
    strictness, not a usability bug: the deployed front maps the bound port
    (``tailscale serve --https=<port>``), so the owner's browser sends the
    port-qualified Origin and matches.
    """
    with pytest.raises(OriginRefusal) as excinfo:
        require_origin(
            _request(_origin_headers(f"https://{_TAILNET_HOST}", host=f"127.0.0.1:{_PORT}")),
            bound_port=_PORT,
            allowed_paired_origins=(("https", _TAILNET_HOST),),
        )
    assert excinfo.value.status_code == 403
    assert excinfo.value.triple.missed_field == "port"


def test_expected_patterns_include_paired_entry_only_when_present() -> None:
    assert expected_origin_patterns(bound_port=_PORT) == (
        f"http://127.0.0.1:{_PORT}",
        f"http://localhost:{_PORT}",
    )
    with_paired = expected_origin_patterns(
        bound_port=_PORT, allowed_paired_origins=(("https", _TAILNET_HOST),)
    )
    assert f"https://{_TAILNET_HOST}:{_PORT}" in with_paired


# --- classify_origin_miss (Part 3, Acceptance #5) ---------------------------


def test_classify_origin_miss_labels_each_field() -> None:
    assert classify_origin_miss(f"https://127.0.0.1:{_PORT}", bound_port=_PORT) == "scheme"
    assert classify_origin_miss(f"http://evil.example:{_PORT}", bound_port=_PORT) == "host"
    assert classify_origin_miss(f"http://127.0.0.1:{_PORT + 1}", bound_port=_PORT) == "port"
    assert classify_origin_miss(f"https://evil.example:{_PORT}", bound_port=_PORT) == "no_match"
    assert classify_origin_miss(None, bound_port=_PORT) == "missing"


def test_classify_origin_miss_treats_cross_product_as_no_match() -> None:
    paired = (("https", _TAILNET_HOST),)
    assert (
        classify_origin_miss(
            f"https://127.0.0.1:{_PORT}", bound_port=_PORT, allowed_paired_origins=paired
        )
        == "no_match"
    )


# --- is_via_front -----------------------------------------------------------


def test_is_via_front_keys_on_tailscale_identity_header() -> None:
    assert is_via_front(_request({"tailscale-user-login": "owner@example.com"})) is True
    assert is_via_front(_request({"host": f"127.0.0.1:{_PORT}"})) is False


# --- RefusalTriple rendering ------------------------------------------------


def _triple() -> RefusalTriple:
    return RefusalTriple(
        rule="origin_mismatch",
        missed_field="host",
        expected=(f"http://127.0.0.1:{_PORT}",),
        remedy="add it to BINKEEPER_OPERATOR_ALLOWED_ORIGINS",
    )


def test_refusal_header_grammar_is_pinned() -> None:
    triple = _triple()
    assert triple.header_value(verbose=True) == "origin_mismatch; field=host"
    assert triple.header_value(verbose=False) == "origin_mismatch"


def test_refusal_json_body_is_dual_shape_and_terse_over_front() -> None:
    triple = _triple()
    verbose = triple.json_body(verbose=True)
    assert verbose["error"] == "origin_mismatch"
    assert isinstance(verbose["detail"], dict)
    assert verbose["detail"]["error"] == "origin_mismatch"
    assert verbose["missed_field"] == "host"
    assert "expected" in verbose and "remedy" in verbose

    terse = triple.json_body(verbose=False)
    assert terse["error"] == "origin_mismatch"
    assert "missed_field" not in terse and "expected" not in terse and "remedy" not in terse
    assert terse["detail"] == {"error": "origin_mismatch"}


def test_refusal_html_is_inert() -> None:
    page = _triple().html_page(verbose=True)
    assert "<script" not in page.lower()
    assert "http-equiv" not in page.lower()


# --- never reflect the raw Origin (Part 3, Acceptance #3/#5) ----------------


@pytest.mark.parametrize(
    "hostile_origin",
    [
        '"><script>alert(1)</script>',
        f"http://127.0.0.1:{_PORT}\r\nX-Injected: 1",
    ],
)
def test_hostile_origin_is_never_reflected(hostile_origin: str) -> None:
    request = _request(_origin_headers(hostile_origin))
    with pytest.raises(OriginRefusal) as excinfo:
        require_origin(request, bound_port=_PORT)

    # Render to both loopback (verbose) and HTML, and assert the raw Origin
    # appears in neither the header nor the page.
    loopback = _request({"host": f"127.0.0.1:{_PORT}", "accept": "text/html"})
    response = render_refusal_response(loopback, excinfo.value)
    header_value = response.headers[REFUSAL_HEADER]
    body = bytes(response.body).decode("utf-8")

    for needle in ("<script>", "alert(1)", "X-Injected"):
        assert needle not in header_value
        assert needle not in body
    assert "\r" not in header_value and "\n" not in header_value


def test_render_is_terse_over_front_and_verbose_on_loopback() -> None:
    triple = _triple()
    exc = OriginRefusal(status_code=403, triple=triple)

    front = render_refusal_response(_request({"tailscale-user-login": "owner@example.com"}), exc)
    assert front.headers[REFUSAL_HEADER] == "origin_mismatch"
    assert b"remedy" not in bytes(front.body)

    loopback = render_refusal_response(_request({"host": f"127.0.0.1:{_PORT}"}), exc)
    assert loopback.headers[REFUSAL_HEADER] == "origin_mismatch; field=host"
    assert b"remedy" in bytes(loopback.body)


# --- matches_paired_front (shared Host-based paired-front check, item #7) ----


def _paired_front_headers(host: str, *, forwarded_proto: str | None = None) -> dict[str, str]:
    headers = {"host": host}
    if forwarded_proto is not None:
        headers["x-forwarded-proto"] = forwarded_proto
    return headers


def test_matches_paired_front_true_for_paired_host_port_and_proto() -> None:
    assert matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:{_PORT}", forwarded_proto="https")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )


def test_matches_paired_front_true_when_front_stamps_no_proto() -> None:
    # X-Forwarded-Proto absent => the proto clause is satisfied (None-tolerant).
    assert matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:{_PORT}")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )


def test_matches_paired_front_false_without_allowed_origins() -> None:
    assert not matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:{_PORT}", forwarded_proto="https")),
        allowed_paired_origins=(),
        bound_port=_PORT,
    )


def test_matches_paired_front_false_on_wrong_port() -> None:
    assert not matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:{_PORT + 1}", forwarded_proto="https")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )


def test_matches_paired_front_false_on_proto_mismatch() -> None:
    assert not matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:{_PORT}", forwarded_proto="http")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )


def test_matches_paired_front_false_on_unpaired_host() -> None:
    assert not matches_paired_front(
        _request(_paired_front_headers(f"other.ts.net:{_PORT}", forwarded_proto="https")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )


def test_matches_paired_front_false_on_malformed_port() -> None:
    # A non-numeric port makes urlsplit(...).port raise ValueError -> refuse.
    assert not matches_paired_front(
        _request(_paired_front_headers(f"{_TAILNET_HOST}:notaport")),
        allowed_paired_origins=(("https", _TAILNET_HOST),),
        bound_port=_PORT,
    )
