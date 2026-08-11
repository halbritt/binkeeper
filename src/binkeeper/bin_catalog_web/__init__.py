"""Read-only owner-facing BinKeeper catalog."""

from __future__ import annotations

import binascii
import json
import logging
from collections.abc import Callable, Mapping, Sequence, Set
from functools import partial
from importlib import resources
from pathlib import Path
from typing import Final, Literal, Protocol, TypedDict
from urllib.parse import quote

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as ApiPath
from fastapi.responses import HTMLResponse, Response
from PIL import Image

from binkeeper.bin_colocation import ContainmentBelief, containment_by_bin
from binkeeper.bin_inventory import BinInventoryError
from binkeeper.bin_label_drift import LabelDriftError, LabelDriftQueueEntry
from binkeeper.bin_passport import (
    BIN_CAPTURE_KIND,
    BinPassport,
    BinPassportError,
    BinVolumeProfile,
    load_bin_passports,
)
from binkeeper.bin_photo_media import thumbnail_jpeg
from binkeeper.blob_vault import (
    BlobVaultError,
    blob_store_from_config,
    open_blob,
    vault_key_from_config,
)
from binkeeper.db import ServingRoleUnavailableError, connect
from binkeeper.web.chrome import build_surface_chrome, mount_shared_static, page_response
from binkeeper.web.origin import (
    PairedOrigin,
    install_origin_refusal_handler,
    is_direct_loopback_request,
    matches_paired_front,
)
from binkeeper.web.paths import normalize_base_path, surface_path

BIN_CATALOG_REFUSAL_RULE: Final[str] = "bin_catalog_loopback_only"
_TEMPLATE_DIR: Final[Path] = Path(str(resources.files("binkeeper.bin_catalog_web") / "templates"))
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})

PassportLoader = Callable[[], Sequence[BinPassport]]
LabelDriftLoader = Callable[[], Sequence[LabelDriftQueueEntry]]
PhotoState = Literal["available", "missing", "unavailable"]
logger = logging.getLogger(__name__)


class BinCatalogUnavailableError(RuntimeError):
    """Raised when the read-only catalog projection cannot be loaded."""


class BinCatalogPhotoError(RuntimeError):
    """Raised when a capture-linked catalog photo cannot be read safely."""


class BinCatalogPhotoSource(Protocol):
    """Read capture-linked originals without exposing vault identifiers."""

    def linked_bin_codes(self, bin_codes: Sequence[str]) -> Set[str]:
        """Return the requested bin codes that carry a photo reference."""
        ...

    def load_original(self, bin_code: str) -> bytes | None:
        """Return the latest capture-linked original for one exact bin code."""
        ...


class _ConfiguredCatalogPhotos:
    """Read capture-linked originals from the configured local vault."""

    def __init__(self, *, tenant_id: str, corpus_id: str) -> None:
        self._tenant_id = tenant_id
        self._corpus_id = corpus_id

    def linked_bin_codes(self, bin_codes: Sequence[str]) -> Set[str]:
        if not bin_codes:
            return frozenset()
        try:
            with connect(role="serving") as conn:
                rows = conn.execute(
                    """
                    SELECT DISTINCT raw_payload->'metadata'->>'bin_code'
                    FROM captures
                    WHERE tenant_id = %s
                      AND corpus_id = %s
                      AND raw_payload->'metadata'->>'kind' = %s
                      AND raw_payload->'metadata'->>'bin_code' = ANY(%s)
                      AND NULLIF(btrim(COALESCE(
                          raw_payload->'metadata'->'photo'->>'blob_ref',
                          raw_payload->'metadata'->'photo'->>'sha256'
                      )), '') IS NOT NULL
                    """,
                    (self._tenant_id, self._corpus_id, BIN_CAPTURE_KIND, list(bin_codes)),
                ).fetchall()
        except (ServingRoleUnavailableError, psycopg.Error) as exc:
            raise BinCatalogPhotoError("could not read catalog photo links") from exc
        return frozenset(str(row[0]) for row in rows if row[0])

    def load_original(self, bin_code: str) -> bytes | None:
        try:
            with connect(role="serving") as conn:
                row = conn.execute(
                    """
                    SELECT NULLIF(btrim(COALESCE(
                        raw_payload->'metadata'->'photo'->>'blob_ref',
                        raw_payload->'metadata'->'photo'->>'sha256'
                    )), '')
                    FROM captures
                    WHERE tenant_id = %s
                      AND corpus_id = %s
                      AND raw_payload->'metadata'->>'kind' = %s
                      AND raw_payload->'metadata'->>'bin_code' = %s
                      AND NULLIF(btrim(COALESCE(
                          raw_payload->'metadata'->'photo'->>'blob_ref',
                          raw_payload->'metadata'->'photo'->>'sha256'
                      )), '') IS NOT NULL
                    ORDER BY COALESCE(observed_at, imported_at) DESC,
                             imported_at DESC,
                             external_id DESC
                    LIMIT 1
                    """,
                    (self._tenant_id, self._corpus_id, BIN_CAPTURE_KIND, bin_code),
                ).fetchone()
                if row is None or not row[0]:
                    return None
                store = blob_store_from_config()
                key, _key_ref = vault_key_from_config()
                return open_blob(
                    conn,
                    store,
                    str(row[0]),
                    key=key,
                    tenant_id=self._tenant_id,
                    corpus_id=self._corpus_id,
                )
        except (
            BlobVaultError,
            binascii.Error,
            json.JSONDecodeError,
            OSError,
            ServingRoleUnavailableError,
            psycopg.Error,
        ) as exc:
            raise BinCatalogPhotoError("could not read catalog photo") from exc


class BinCatalogEntry(TypedDict):
    """Template-safe catalog fields selected from one folded passport."""

    bin_code: str
    theme: str | None
    current_site_label: str
    current_site_key: str
    home_site_label: str
    owner_phrase: str | None
    current_contents: str | None
    physical_constraints: tuple[str, ...]
    capacity_label: str
    volume_label: str
    location_confidence: float
    location_confidence_label: str
    fog_level: str
    fog_label: str
    passport_confidence_label: str
    photo_state: PhotoState
    photo_url: str | None
    manage_url: str | None
    anchor_label: str | None
    containment_label: str | None
    contained_bin_codes: tuple[str, ...]
    search_text: str


class SiteOption(TypedDict):
    """One current-site option for the catalog filter."""

    key: str
    label: str
    visibility_pct: int


def require_bin_catalog_request(
    request: Request,
    *,
    allowed_paired_origins: tuple[PairedOrigin, ...] = (),
    bound_port: int,
) -> None:
    """Allow direct loopback or the one owner-paired tailnet front (D136)."""
    if is_direct_loopback_request(request):
        return
    if matches_paired_front(
        request,
        allowed_paired_origins=allowed_paired_origins,
        bound_port=bound_port,
    ):
        return
    raise HTTPException(
        status_code=409,
        detail=(
            "the bin catalog renders for direct loopback requests or the "
            "owner-paired tailnet front only (D136)"
        ),
        headers={"X-BinKeeper-Refusal": BIN_CATALOG_REFUSAL_RULE},
    )


def create_app(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    base_path: str = "",
    allowed_paired_origins: tuple[PairedOrigin, ...] = (),
    tenant_id: str = "personal",
    corpus_id: str = "personal",
    authoring_enabled: bool = False,
    authoring_base_path: str = "/bin-photo",
    passport_loader: PassportLoader | None = None,
    photo_source: BinCatalogPhotoSource | None = None,
    containment_loader: ContainmentLoader | None = None,
    virtual_loader: VirtualLoader | None = None,
    label_drift_loader: LabelDriftLoader | None = None,
) -> FastAPI:
    """Build the loopback-bound, paired-tailnet-safe BinKeeper catalog app."""
    normalized_host = host.strip()
    if normalized_host not in _LOOPBACK_HOSTS:
        raise ValueError(f"bin catalog web refuses non-loopback host: {host}")
    normalized_base_path = normalize_base_path(base_path)
    normalized_authoring_base_path = normalize_base_path(authoring_base_path)
    paired_origins = tuple(allowed_paired_origins)
    bound_port = int(port)

    def catalog_request_gate(request: Request) -> None:
        require_bin_catalog_request(
            request,
            allowed_paired_origins=paired_origins,
            bound_port=bound_port,
        )

    app = FastAPI(
        title="BinKeeper BinKeeper catalog",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(catalog_request_gate)],
    )
    app.state.binkeeper_base_path = normalized_base_path
    app.state.allowed_paired_origins = paired_origins
    app.state.binkeeper_bound_port = bound_port
    install_origin_refusal_handler(app)
    mount_shared_static(app)
    chrome = build_surface_chrome(
        "bin_catalog",
        base_path=normalized_base_path,
        extra_template_dir=_TEMPLATE_DIR,
    )
    load_passports = passport_loader or partial(
        _load_passports,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    load_containment = containment_loader or partial(
        _load_containment,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    load_virtual = virtual_loader or partial(
        _load_virtual_bins,
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )
    if label_drift_loader is not None:
        load_label_drift = label_drift_loader
    elif passport_loader is not None:
        # A supplied passport loader defines a complete synthetic/read-model
        # boundary (the browser harness relies on this). Do not reach around it
        # into the production database for a second projection.
        load_label_drift = lambda: ()
    else:
        load_label_drift = partial(
            _load_label_drift_queue,
            tenant_id=tenant_id,
            corpus_id=corpus_id,
        )
    catalog_photos = photo_source or _ConfiguredCatalogPhotos(
        tenant_id=tenant_id,
        corpus_id=corpus_id,
    )

    def path(suffix: str = "/") -> str:
        return surface_path(normalized_base_path, suffix)

    def authoring_path(suffix: str = "/") -> str:
        return surface_path(normalized_authoring_base_path, suffix)

    shell_context: dict[str, object] = {
        "surface_label": "Bin catalog",
        "bind_address": f"{normalized_host}:{bound_port}",
        "surface_path": path,
        "catalog_url": path("/"),
        "photo_url": authoring_path("/"),
        "register_url": authoring_path("/register"),
        "stash_url": authoring_path("/stash"),
        "binkeeper_section": "catalog",
        "authoring_enabled": authoring_enabled,
        "help_title": "Bin catalog help",
        "verdict_help_rows": (),
        "shortcut_rows": (("/", "Focus catalog search"), ("?", "Open help")),
        "disclosure_lines": (
            "Read-only BinKeeper passports from local evidence. Current sites use "
            "append-only move history, with the latest legacy capture as fallback.",
        ),
    }

    @app.get("/")
    def catalog(
        q: str = Query(default="", max_length=160),
        site: str = Query(default="", max_length=120),
    ) -> HTMLResponse:
        query = q.strip()
        selected_site = site.strip()
        try:
            passports = sorted(load_passports(), key=lambda passport: passport.bin_code.casefold())
            label_drift_queue = list(load_label_drift())
            try:
                witnessed = load_containment()
            except BinCatalogUnavailableError:
                witnessed = {}
            try:
                virtual_bins = load_virtual()
            except BinCatalogUnavailableError:
                virtual_bins = []
            try:
                linked_photo_bins = catalog_photos.linked_bin_codes(
                    [passport.bin_code for passport in passports]
                )
                photo_lookup_unavailable = False
            except BinCatalogPhotoError:
                linked_photo_bins = frozenset()
                photo_lookup_unavailable = True
            all_entries = [
                _passport_view(
                    passport,
                    anchor_label=_anchor_label(witnessed.get(passport.bin_code)),
                    photo_state=(
                        "unavailable"
                        if photo_lookup_unavailable
                        else "available"
                        if passport.bin_code in linked_photo_bins
                        else "missing"
                    ),
                    photo_url=(
                        path(f"/photo/{quote(passport.bin_code, safe='')}")
                        if passport.bin_code in linked_photo_bins
                        else None
                    ),
                    manage_url=(
                        authoring_path(f"/manage/{quote(passport.bin_code, safe='')}")
                        if authoring_enabled
                        else None
                    ),
                )
                for passport in passports
            ]
        except BinCatalogUnavailableError:
            # Deliberately omit exception text: database errors can contain local
            # connection details that do not belong in either HTML or the journal.
            logger.warning("bin catalog projection is unavailable")
            return page_response(
                chrome.render(
                    "bin_catalog.html",
                    **shell_context,
                    entries=(),
                    sites=(),
                    total_count=0,
                    contents_count=0,
                    visibility_pct=0,
                    label_drift_entries=(),
                    label_drift_count=0,
                    virtual_bins=[],
                    query=query,
                    selected_site=selected_site,
                    catalog_unavailable=True,
                ),
                status_code=503,
            )

        sites = _site_options(all_entries)
        normalized_query = query.casefold()
        entries = [
            entry
            for entry in all_entries
            if (not normalized_query or normalized_query in entry["search_text"])
            and (not selected_site or selected_site == entry["current_site_key"])
        ]
        contents_count = sum(1 for entry in all_entries if entry["current_contents"])
        label_drift_entries = [
            {
                "bin_code": entry.bin_code,
                "proposed_theme": entry.proposed_theme,
                "new_item_labels": entry.new_item_labels,
                "manage_url": (
                    authoring_path(f"/manage/{quote(entry.bin_code, safe='')}#label-drift-review")
                    if authoring_enabled
                    else None
                ),
            }
            for entry in label_drift_queue
        ]
        return page_response(
            chrome.render(
                "bin_catalog.html",
                **shell_context,
                entries=entries,
                sites=sites,
                total_count=len(all_entries),
                contents_count=contents_count,
                visibility_pct=_visibility_pct(all_entries, site_key=selected_site or None),
                label_drift_entries=label_drift_entries,
                label_drift_count=len(label_drift_entries),
                virtual_bins=virtual_bins,
                query=query,
                selected_site=selected_site,
                catalog_unavailable=False,
            )
        )

    @app.get("/photo/{bin_code}")
    def catalog_photo(
        bin_code: str = ApiPath(min_length=1, max_length=120),
    ) -> Response:
        try:
            original = catalog_photos.load_original(bin_code.strip())
            if original is None:
                raise BinCatalogPhotoError("catalog photo is not linked")
            thumbnail = thumbnail_jpeg(original)
        except (
            BinCatalogPhotoError,
            Image.DecompressionBombError,
            OSError,
            SyntaxError,
            ValueError,
        ):
            raise HTTPException(
                status_code=404,
                detail="catalog photo is unavailable",
                headers=_private_photo_headers(),
            ) from None
        return Response(
            content=thumbnail,
            media_type="image/jpeg",
            headers=_private_photo_headers(),
        )

    return app


def _private_photo_headers() -> dict[str, str]:
    """Return response headers that prevent thumbnail caching and sniffing."""
    return {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


def _passport_view(
    passport: BinPassport,
    *,
    photo_state: PhotoState,
    photo_url: str | None,
    manage_url: str | None,
    anchor_label: str | None = None,
) -> BinCatalogEntry:
    """Select and format only owner-safe fields for the catalog template."""
    contents = _unique(passport.sibling_contents)
    current_site_key = passport.current_site or ""
    searchable_values = (
        passport.bin_code,
        passport.theme or "",
        passport.current_site or "",
        passport.home_site or "",
        passport.owner_phrase or "",
        *contents,
        *passport.containment_path,
        *passport.contained_bin_codes,
    )
    return {
        "bin_code": passport.bin_code,
        "theme": passport.theme,
        "current_site_label": _site_label(passport.current_site),
        "current_site_key": current_site_key,
        "home_site_label": _site_label(passport.home_site, missing="Not recorded"),
        "owner_phrase": passport.owner_phrase,
        "current_contents": contents[-1] if contents else None,
        "physical_constraints": _unique(passport.physical_constraints),
        "capacity_label": _capacity_label(passport.capacity_state),
        "volume_label": _volume_label(passport.volume_profile),
        "location_confidence": passport.location_confidence,
        "location_confidence_label": _confidence_label(passport.location_confidence),
        "fog_level": _fog_level(passport.location_confidence),
        "fog_label": _FOG_LABELS[_fog_level(passport.location_confidence)],
        "passport_confidence_label": _confidence_label(passport.passport_confidence),
        "photo_state": photo_state,
        "photo_url": photo_url,
        "manage_url": manage_url,
        "anchor_label": anchor_label,
        "containment_label": (
            f"Inside {' → '.join(passport.containment_path)}" if passport.containment_path else None
        ),
        "contained_bin_codes": passport.contained_bin_codes,
        "search_text": " ".join(searchable_values).casefold(),
    }


def _site_options(entries: Sequence[BinCatalogEntry]) -> tuple[SiteOption, ...]:
    labels = {
        entry["current_site_key"]: entry["current_site_label"]
        for entry in entries
        if entry["current_site_key"]
    }
    return tuple(
        {"key": key, "label": label, "visibility_pct": _visibility_pct(entries, site_key=key)}
        for key, label in sorted(labels.items(), key=lambda site_pair: site_pair[1].casefold())
    )


# Fog-of-war buckets over decayed location confidence (BINK-21): staleness is
# rendered as visible fog to reclaim, not silent rot. A confirm or fetch event
# refreshes confidence, so the tile un-greys on the next render.
_FOG_LABELS: Final[dict[str, str]] = {
    "fresh": "Location fresh",
    "aging": "Location fading",
    "stale": "Location stale",
    "fog": "Location in fog",
}


def _fog_level(confidence: float) -> str:
    if confidence >= 0.8:
        return "fresh"
    if confidence >= 0.6:
        return "aging"
    if confidence >= 0.4:
        return "stale"
    return "fog"


def _visibility_pct(entries: Sequence[BinCatalogEntry], *, site_key: str | None = None) -> int:
    """Mean location confidence (percent) over the entries, optionally one site."""
    confidences = [
        entry["location_confidence"]
        for entry in entries
        if site_key is None or entry["current_site_key"] == site_key
    ]
    if not confidences:
        return 0
    return round(100 * sum(confidences) / len(confidences))


def _unique(text_values: Sequence[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(text_value.strip() for text_value in text_values if text_value.strip())
    )


def _site_label(site: str | None, *, missing: str = "Unknown") -> str:
    if not site:
        return missing
    return site.replace("-", " ").replace("_", " ").capitalize()


def _capacity_label(capacity_state: str) -> str:
    if capacity_state == "unknown":
        return "Capacity not recorded"
    return capacity_state.replace("_", " ").capitalize()


def _volume_label(profile: BinVolumeProfile | None) -> str:
    if profile is None:
        return "Volume not recorded"
    if profile.volume_label:
        return profile.volume_label
    if profile.volume_value is not None and profile.volume_unit != "unknown":
        return f"{profile.volume_value:g} {profile.volume_unit}"
    if profile.canonical_volume_liters is not None:
        return f"{profile.canonical_volume_liters:g} L"
    if profile.form_factor:
        return profile.form_factor.replace("_", " ").capitalize()
    return "Volume details recorded"


def _confidence_label(confidence: float) -> str:
    return f"{round(max(0.0, min(1.0, confidence)) * 100)}%"


ContainmentLoader = Callable[[], "Mapping[str, ContainmentBelief]"]
VirtualLoader = Callable[[], list[dict[str, object]]]


def _load_virtual_bins(*, tenant_id: str, corpus_id: str) -> list[dict[str, object]]:
    """Compute virtual bins through the least-privilege serving role."""
    from binkeeper.bin_virtual import list_virtual_bins

    try:
        with connect(role="serving") as conn:
            return [
                bin.to_json()
                for bin in list_virtual_bins(conn, tenant_id=tenant_id, corpus_id=corpus_id)
            ]
    except (ServingRoleUnavailableError, psycopg.Error) as exc:
        raise BinCatalogUnavailableError("could not load virtual bins") from exc


def _anchor_label(belief: ContainmentBelief | None) -> str | None:
    """Render the shelf tier for the details list, or None when abstained."""
    if belief is None:
        return None
    tier = belief.to_json()
    if tier is None:
        return None
    age = tier["age_days"]
    age_text = f", seen {age:g}d ago" if isinstance(age, int | float) else ""
    return f"Last witnessed on {tier['anchor_code']} ({tier['confidence']:.0%}{age_text})"


def _load_containment(*, tenant_id: str, corpus_id: str) -> Mapping[str, ContainmentBelief]:
    """Fold witnessed shelves through the least-privilege serving role."""
    try:
        with connect(role="serving") as conn:
            return containment_by_bin(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    except (ServingRoleUnavailableError, psycopg.Error) as exc:
        raise BinCatalogUnavailableError("could not load witnessed shelves") from exc


def _load_passports(*, tenant_id: str, corpus_id: str) -> Sequence[BinPassport]:
    """Load folded bin passports through the least-privilege serving role."""
    try:
        with connect(role="serving") as conn:
            return load_bin_passports(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    except (
        BinInventoryError,
        BinPassportError,
        ServingRoleUnavailableError,
        psycopg.Error,
    ) as exc:
        raise BinCatalogUnavailableError("could not load bin catalog") from exc


def _load_label_drift_queue(*, tenant_id: str, corpus_id: str) -> Sequence[LabelDriftQueueEntry]:
    """Rebuild pending label reviews through the least-privilege serving role."""
    from binkeeper.bin_label_drift import load_label_drift_queue

    try:
        with connect(role="serving") as conn:
            return load_label_drift_queue(conn, tenant_id=tenant_id, corpus_id=corpus_id)
    except (LabelDriftError, ServingRoleUnavailableError, psycopg.Error) as exc:
        raise BinCatalogUnavailableError("could not load label-drift queue") from exc
