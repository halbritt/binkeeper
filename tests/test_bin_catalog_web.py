"""Owner-facing read-only BinKeeper catalog route."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import ExifTags, Image

from binkeeper import bin_catalog_web
from binkeeper.bin_catalog_web import BinCatalogPhotoError, BinCatalogUnavailableError, create_app
from binkeeper.bin_passport import BinPassport


class _PhotoSource:
    """Test boundary for capture-linked original photos."""

    def __init__(self, originals: dict[str, bytes]) -> None:
        self._originals = originals
        self.load_calls: list[str] = []

    def linked_bin_codes(self, bin_codes: Sequence[str]) -> frozenset[str]:
        return frozenset(self._originals).intersection(bin_codes)

    def load_original(self, bin_code: str) -> bytes | None:
        self.load_calls.append(bin_code)
        return self._originals.get(bin_code)


class _UnavailablePhotoSource:
    """Test boundary for a vault/index read outage."""

    def linked_bin_codes(self, bin_codes: Sequence[str]) -> frozenset[str]:
        raise BinCatalogPhotoError("private vault detail")

    def load_original(self, bin_code: str) -> bytes | None:
        raise BinCatalogPhotoError("private vault detail")


def _large_jpeg() -> bytes:
    output = BytesIO()
    exif = Image.Exif()
    exif[0x010E] = "private catalog test metadata"
    Image.new("RGB", (1600, 1200), "navy").save(output, format="JPEG", exif=exif)
    return output.getvalue()


def _oriented_gps_jpeg() -> bytes:
    output = BytesIO()
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.IFD.GPSInfo] = {
        1: "N",
        2: (37.0, 0.0, 0.0),
        3: "W",
        4: (122.0, 0.0, 0.0),
    }
    Image.new("RGB", (120, 80), "navy").save(output, format="JPEG", exif=exif)
    return output.getvalue()


def _passport() -> BinPassport:
    return BinPassport(
        bin_code="AGR-014",
        theme="Precision tools",
        home_site="alameda-garage",
        current_site="oakland-fab-east",
        owner_phrase="Keep measuring tools together",
        accepts=("PRIVATE-ACCEPT-SENTINEL",),
        excludes=("PRIVATE-EXCLUDE-SENTINEL",),
        examples=("PRIVATE-EXAMPLE-SENTINEL",),
        sibling_contents=("hex keys and digital calipers",),
        physical_constraints=("keep dry",),
        volume_profile=None,
        capacity_state="half",
        location_confidence=0.96,
        passport_confidence=0.88,
        provenance_refs=("capture:bin-1#metadata.contents_text",),
    )


def _catalog_passports() -> tuple[BinPassport, BinPassport]:
    paint_bin = replace(
        _passport(),
        bin_code="AGR-002",
        theme="Paint supplies",
        current_site="alameda-garage",
        sibling_contents=("rollers and masking tape",),
        owner_phrase="Keep paint supplies together",
        accepts=("paint supplies",),
        examples=("masking tape",),
    )
    return (_passport(), paint_bin)


def test_catalog_shows_bin_codes_contents_and_locations() -> None:
    app = create_app(
        base_path="/bins",
        authoring_enabled=True,
        passport_loader=lambda: [_passport()],
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "Bin catalog" in response.text
    assert "AGR-014" in response.text
    assert "hex keys and digital calipers" in response.text
    assert "Oakland fab east" in response.text
    assert 'type="search"' in response.text
    assert 'id="site-filter"' in response.text
    assert "Keep measuring tools together" in response.text
    assert "PRIVATE-ACCEPT-SENTINEL" not in response.text
    assert "PRIVATE-EXCLUDE-SENTINEL" not in response.text
    assert "PRIVATE-EXAMPLE-SENTINEL" not in response.text
    assert "Half" in response.text
    assert "Volume not recorded" in response.text
    assert "96%" in response.text
    assert "88%" in response.text
    assert 'data-search-text="' in response.text
    assert 'href="/bin-photo/"' in response.text
    assert 'href="/bin-photo/register"' in response.text
    assert 'href="/bin-photo/manage/AGR-014"' in response.text
    assert "Manage bin" in response.text
    assert ">Contents<" in response.text
    assert "Recorded contents" not in response.text
    assert "https://" not in response.text


def test_catalog_navigation_stays_inside_binkeeper() -> None:
    app = create_app(
        base_path="/bins",
        authoring_enabled=True,
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'aria-label="BinKeeper"' in response.text
    assert 'data-binkeeper-section="catalog" aria-current="page"' in response.text
    assert 'href="/bins/"' in response.text
    assert 'href="/bin-photo/"' in response.text
    assert 'href="/bin-photo/register"' in response.text
    assert ">Interview<" not in response.text
    assert ">Context quiz<" not in response.text
    assert "Operator home" not in response.text


def test_catalog_serves_a_bounded_metadata_free_thumbnail_for_a_linked_bin() -> None:
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({"AGR-014": _large_jpeg()}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/photo/AGR-014")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    with Image.open(BytesIO(response.content)) as thumbnail:
        assert thumbnail.size == (960, 720)
        assert len(thumbnail.getexif()) == 0


def test_catalog_applies_orientation_and_strips_gps_from_a_linked_photo() -> None:
    app = create_app(
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({"AGR-014": _oriented_gps_jpeg()}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/photo/AGR-014")

    assert response.status_code == 200
    with Image.open(BytesIO(response.content)) as thumbnail:
        assert thumbnail.size == (80, 120)
        assert len(thumbnail.getexif()) == 0


# Regression for the owner-visible 2026-07-14 transient photo activation.
def test_catalog_renders_a_safe_lazy_preview_for_a_capture_linked_bin() -> None:
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({"AGR-014": _large_jpeg()}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'data-photo-state="available"' in response.text
    assert 'src="/bins/photo/AGR-014"' in response.text
    assert 'alt="Stored bin photo for AGR-014"' in response.text
    assert 'loading="lazy" decoding="async"' in response.text
    assert 'draggable="false"' in response.text
    assert 'aria-label="View stored photo for AGR-014"' in response.text
    assert "data-photo-preview" in response.text
    assert 'aria-labelledby="bin-photo-dialog-heading"' in response.text
    assert "data-photo-dialog" in response.text
    assert "object-fit: contain" in response.text
    assert "plaintext_sha256" not in response.text
    assert "blob_ref" not in response.text


def test_catalog_marks_a_bin_without_a_capture_link_as_missing() -> None:
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
        photo = client.get("/photo/AGR-014")

    assert response.status_code == 200
    assert 'data-photo-state="missing"' in response.text
    assert "No photo recorded" in response.text
    assert "/bins/photo/AGR-014" not in response.text
    assert photo.status_code == 404
    assert photo.json() == {"detail": "catalog photo is unavailable"}
    assert photo.headers["cache-control"] == "private, no-store"
    assert photo.headers["x-content-type-options"] == "nosniff"


def test_catalog_marks_photo_lookup_failure_as_unavailable_without_details() -> None:
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        photo_source=_UnavailablePhotoSource(),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
        photo = client.get("/photo/AGR-014")

    assert response.status_code == 200
    assert 'data-photo-state="unavailable"' in response.text
    assert "Photo unavailable" in response.text
    assert "private vault detail" not in response.text
    assert photo.status_code == 404
    assert "private vault detail" not in photo.text


def test_catalog_degrades_a_linked_but_unreadable_photo_to_a_safe_fallback() -> None:
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        photo_source=_PhotoSource({"AGR-014": b"private corrupt original"}),
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/")
        photo = client.get("/photo/AGR-014")

    assert page.status_code == 200
    assert "data-bin-photo" in page.text
    assert "data-photo-unavailable hidden" in page.text
    assert "Photo unavailable" in page.text
    assert photo.status_code == 404
    assert photo.json() == {"detail": "catalog photo is unavailable"}
    assert b"private corrupt original" not in photo.content


def test_default_catalog_photo_source_reads_the_capture_linked_local_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = "a" * 64
    roles: list[str | None] = []
    opened: list[str] = []

    class _Result:
        def __init__(self, rows: list[tuple[str, ...]]) -> None:
            self._rows = rows

        def fetchall(self) -> list[tuple[str, ...]]:
            return self._rows

        def fetchone(self) -> tuple[str, ...] | None:
            return self._rows[0] if self._rows else None

    class _Connection:
        def execute(self, query: str, params: object) -> _Result:
            if "SELECT DISTINCT" in query:
                return _Result([("AGR-014",)])
            return _Result([(digest,)])

    @contextmanager
    def fake_connect(*, role: str | None = None) -> Iterator[_Connection]:
        roles.append(role)
        yield _Connection()

    def fake_open_blob(
        conn: object,
        store: object,
        plaintext_sha256: str,
        *,
        key: bytes,
        tenant_id: str,
        corpus_id: str,
    ) -> bytes:
        opened.append(plaintext_sha256)
        return _large_jpeg()

    monkeypatch.setattr(bin_catalog_web, "connect", fake_connect)
    monkeypatch.setattr(bin_catalog_web, "blob_store_from_config", lambda: object(), raising=False)
    monkeypatch.setattr(
        bin_catalog_web,
        "vault_key_from_config",
        lambda: (b"k" * 32, "local-test-key"),
        raising=False,
    )
    monkeypatch.setattr(bin_catalog_web, "open_blob", fake_open_blob, raising=False)
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        containment_loader=lambda: {},
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/")
        photo = client.get("/photo/AGR-014")

    assert page.status_code == 200
    assert 'data-photo-state="available"' in page.text
    assert photo.status_code == 200
    assert roles == ["serving", "serving"]
    assert opened == [digest]
    assert digest not in page.text


def test_catalog_refuses_an_unpaired_photo_request_before_reading_the_vault() -> None:
    source = _PhotoSource({"AGR-014": _large_jpeg()})
    paired_host = "proximal.tail0ecc2e.ts.net"
    app = create_app(
        port=8765,
        base_path="/bins",
        allowed_paired_origins=(("https", paired_host),),
        passport_loader=lambda: [_passport()],
        photo_source=source,
    )

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get(
            "/photo/AGR-014",
            headers={
                "host": "intruder.tailnet.ts.net:8765",
                "x-forwarded-proto": "https",
                "x-forwarded-for": "100.64.0.8",
            },
        )

    assert response.status_code == 409
    assert response.headers["X-BinKeeper-Refusal"] == "bin_catalog_loopback_only"
    assert source.load_calls == []


def test_empty_catalog_explains_how_to_add_the_first_bin() -> None:
    app = create_app(base_path="/bins", authoring_enabled=True, passport_loader=lambda: [])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "No bins yet" in response.text
    assert "Register a labeled bin" in response.text
    assert 'href="/bin-photo/"' in response.text
    assert 'href="/bin-photo/register"' in response.text


def test_catalog_hides_authoring_actions_when_authoring_is_unavailable() -> None:
    app = create_app(base_path="/bins", passport_loader=lambda: [_passport()])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'href="/bin-photo/"' not in response.text
    assert 'href="/bin-photo/register"' not in response.text
    assert "/bin-photo/manage/" not in response.text


def test_unavailable_catalog_returns_a_safe_retryable_page() -> None:
    def unavailable() -> list[BinPassport]:
        raise BinCatalogUnavailableError("database password leaked here")

    app = create_app(base_path="/bins", passport_loader=unavailable)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 503
    assert "Catalog unavailable" in response.text
    assert "Reload this page" in response.text
    assert "database password" not in response.text


def test_catalog_accepts_only_loopback_or_the_owner_paired_tailnet_front() -> None:
    calls = 0

    def load() -> list[BinPassport]:
        nonlocal calls
        calls += 1
        return [_passport()]

    paired_host = "proximal.tail0ecc2e.ts.net"
    app = create_app(
        port=8765,
        base_path="/bins",
        allowed_paired_origins=(("https", paired_host),),
        passport_loader=load,
    )
    front_headers = {
        "host": f"{paired_host}:8765",
        "x-forwarded-proto": "https",
        "x-forwarded-for": "100.64.0.7",
    }

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        paired = client.get("/", headers=front_headers)
        refused = client.get(
            "/",
            headers={
                "host": "intruder.tailnet.ts.net:8765",
                "x-forwarded-proto": "https",
                "x-forwarded-for": "100.64.0.8",
            },
        )
        loopback = client.get("/")

    assert paired.status_code == 200
    assert refused.status_code == 409
    assert refused.headers["X-BinKeeper-Refusal"] == "bin_catalog_loopback_only"
    assert loopback.status_code == 200
    assert calls == 2


@pytest.mark.parametrize(
    ("params", "expected_code", "absent_code", "preserved_filter"),
    [
        ({"q": "calipers"}, "AGR-014", "AGR-002", 'value="calipers"'),
        (
            {"site": "alameda-garage"},
            "AGR-002",
            "AGR-014",
            'value="alameda-garage" selected',
        ),
    ],
)
def test_catalog_get_filters_show_only_matching_bins(
    params: dict[str, str],
    expected_code: str,
    absent_code: str,
    preserved_filter: str,
) -> None:
    app = create_app(passport_loader=_catalog_passports)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/", params=params)

    assert expected_code in response.text
    assert absent_code not in response.text
    assert preserved_filter in response.text


def test_catalog_no_match_is_distinct_from_an_empty_catalog() -> None:
    app = create_app(passport_loader=_catalog_passports)

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/", params={"q": "welding helmet"})

    assert response.status_code == 200
    assert "No matching bins" in response.text
    assert "No bins yet" not in response.text


def test_catalog_autoescapes_recorded_owner_text() -> None:
    unsafe = replace(
        _passport(),
        theme='<script src="https://evil.invalid/x.js"></script>',
    )
    app = create_app(passport_loader=lambda: [unsafe])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        page = client.get("/")

    assert page.status_code == 200
    assert "&lt;script" in page.text
    assert '<script src="https://evil.invalid/x.js">' not in page.text


def test_catalog_exposes_no_write_route() -> None:
    app = create_app(passport_loader=lambda: [_passport()])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        post = client.post("/")

    assert post.status_code == 405


def test_catalog_refuses_a_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="refuses non-loopback host"):
        create_app(host="0.0.0.0", passport_loader=lambda: ())


def test_catalog_renders_fog_levels_and_the_visibility_meter() -> None:
    fresh = _passport()  # location_confidence 0.96 -> fresh
    foggy = replace(
        _passport(),
        bin_code="AGR-099",
        current_site="alameda-garage",
        location_confidence=0.3,
    )
    app = create_app(base_path="/bins", passport_loader=lambda: [fresh, foggy])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'data-fog="fresh"' in response.text
    assert 'data-fog="fog"' in response.text
    assert "Location fresh · 96%" in response.text
    assert "Location in fog · 30%" in response.text
    # Overall visibility = mean(0.96, 0.30) = 63%.
    assert "63%" in response.text
    assert "map visibility" in response.text
    assert "% visible" in response.text


def test_catalog_visibility_meter_scopes_to_the_selected_site() -> None:
    fresh = _passport()  # oakland-fab-east, 0.96
    foggy = replace(
        _passport(),
        bin_code="AGR-099",
        current_site="alameda-garage",
        location_confidence=0.3,
    )
    app = create_app(base_path="/bins", passport_loader=lambda: [fresh, foggy])

    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/", params={"site": "alameda-garage"})

    assert response.status_code == 200
    assert ">30%</strong>" in response.text  # the garage's visibility, not the mean


def test_catalog_fresh_confidence_renders_without_fog() -> None:
    app = create_app(base_path="/bins", passport_loader=lambda: [_passport()])
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
    assert 'data-fog="fresh"' in response.text
    assert "Location in fog" not in response.text
    assert "Location stale" not in response.text


def test_catalog_shows_the_witnessed_shelf_line() -> None:
    from binkeeper.bin_colocation import ContainmentBelief

    witnessed = {
        "AGR-014": ContainmentBelief(
            bin_code="AGR-014",
            anchor_code="LOC-014",
            confidence=0.71,
            age_days=9.0,
            observation_count=3,
            abstained=False,
        )
    }
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        containment_loader=lambda: witnessed,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
    assert "Witnessed shelf" in response.text
    assert "Last witnessed on LOC-014 (71%, seen 9d ago)" in response.text


def test_catalog_abstained_shelf_tier_stays_silent() -> None:
    from binkeeper.bin_colocation import ContainmentBelief

    witnessed = {
        "AGR-014": ContainmentBelief(
            bin_code="AGR-014",
            anchor_code="LOC-014",
            confidence=0.2,
            age_days=300.0,
            observation_count=1,
            abstained=True,
        )
    }
    app = create_app(
        base_path="/bins",
        passport_loader=lambda: [_passport()],
        containment_loader=lambda: witnessed,
    )
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get("/")
    assert "Witnessed shelf" not in response.text
