"""Serve synthetic BinKeeper pages for local browser checks, without a database."""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw
from starlette.responses import Response

from binkeeper import bin_photo_web
from binkeeper.bin_catalog_web import create_app as create_catalog_app
from binkeeper.bin_label_drift import LabelDriftQueueEntry
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_photo_web import create_app as create_authoring_app
from binkeeper.bin_photo_web.manage import ManageView
from binkeeper.bin_saved_photos import (
    SavedPhoto,
    SavedPhotoContent,
    SavedPhotoSelectionError,
    SavedPhotosUnavailableError,
)
from binkeeper.web.chrome import build_surface_chrome, page_response


def _passport(code: str, theme: str, contents: str, site: str) -> BinPassport:
    return BinPassport(
        bin_code=code,
        theme=theme,
        home_site="alameda-garage",
        current_site=site,
        owner_phrase="Keep these supplies together",
        accepts=(),
        excludes=(),
        examples=(),
        sibling_contents=(contents,),
        physical_constraints=(),
        volume_profile=None,
        capacity_state="half",
        location_confidence=0.82,
        passport_confidence=0.91,
        provenance_refs=(),
    )


PASSPORTS = (
    _passport("AGR-014", "Precision tools", "hex keys and digital calipers", "alameda-garage"),
    _passport("AGR-022", "Painting supplies", "brushes and masking tape", "oakland-fab-east"),
)
PROPOSAL = LabelDriftQueueEntry(
    proposal_external_id="synthetic-proposal",
    bin_code="AGR-014",
    proposed_at=datetime(2026, 8, 11, 4, tzinfo=UTC),
    current_theme="Precision tools",
    current_contents="hex keys and digital calipers",
    new_item_labels=("tape measure", "steel ruler"),
    photo_hashes=(),
    model_versions=("synthetic-model",),
)
PROPOSALS = (
    PROPOSAL,
    *(
        replace(
            PROPOSAL,
            proposal_external_id=f"synthetic-proposal-{number}",
            bin_code=f"AGR-{number:03d}",
            new_item_labels=("instruction manual", "small boxed parts", "yellow-tipped fastener"),
        )
        for number in range(101, 113)
    ),
)


class _SyntheticPhotos:
    def linked_bin_codes(self, bin_codes: Sequence[str]) -> frozenset[str]:
        return frozenset({"AGR-014"}).intersection(bin_codes)

    def load_original(self, bin_code: str) -> bytes | None:
        return _SYNTHETIC_PHOTO if bin_code == "AGR-014" else None


def _synthetic_photo() -> bytes:
    image = Image.new("RGB", (640, 480), "#e9e5d9")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((80, 145, 560, 415), radius=24, fill="#5d766b")
    draw.rounded_rectangle((65, 120, 575, 185), radius=16, fill="#334c43")
    draw.rectangle((220, 245, 420, 330), fill="#f6f4e9")
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


_SYNTHETIC_PHOTO = _synthetic_photo()


class _SyntheticSavedPhotos:
    def list_photos(self, *, offset: int, limit: int) -> Sequence[SavedPhoto]:
        if offset == 48:
            raise SavedPhotosUnavailableError("Synthetic outage")
        return [
            SavedPhoto(
                str(UUID(int=i + 1)),
                datetime(2026, 11, 8, 16, 39, tzinfo=UTC),
                "AGR-014" if i == 3 else None,
            )
            for i in range(4)
        ][offset : offset + limit]

    def load_photos(self, photo_ids: Sequence[str]) -> Sequence[SavedPhotoContent]:
        if any(UUID(photo_id).int not in range(1, 5) for photo_id in photo_ids):
            raise SavedPhotoSelectionError("Synthetic photo unavailable")
        return [SavedPhotoContent("a" * 64, _SYNTHETIC_PHOTO) for photo_id in photo_ids]


def _manage_view(*, bin_code: str, tenant_id: str, corpus_id: str) -> ManageView:
    return {
        "bin_code": bin_code,
        "theme": "Precision tools",
        "contents": "hex keys and digital calipers",
        "home_site": "alameda-garage",
        "current_site": "alameda-garage",
        "catalog_photo_url": None,
        "container_code": "",
        "containment_path": (),
        "contained_bin_codes": (),
        "container_options": (),
        "label_drift": PROPOSAL,
    }


def create_app(port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def read_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            return JSONResponse({"detail": "synthetic fixture is read-only"}, status_code=405)
        return await call_next(request)

    @app.get("/fixture/photo-result")
    def photo_result(failure: bool = False, stored: bool = True) -> Response:
        chrome = build_surface_chrome(
            "bin_photo", extra_template_dir=Path(bin_photo_web.__file__).parent / "templates"
        )
        return page_response(
            chrome.render(
                "bin_photo_result.html",
                new_href="/",
                saved_photos_url="/saved-photos",
                confirm_action="/register/confirm",
                align_action="/printer/align",
                can_align_label=True,
                b1_available=True,
                catalog_url="/bins/",
                photo_url="/",
                register_url="/register",
                stash_url="/stash",
                binkeeper_section="photo",
                surface_label="Bin photo drop",
                photo_count=1,
                blob_refs=["synthetic-photo"] if stored else [],
                store_error=None if stored else "Vault unavailable",
                proposal=None
                if failure
                else {
                    "theme": "Precision tools",
                    "owner_phrase": "Keep these together",
                    "photo_count": 1,
                    "model_version": "synthetic-model",
                    "summary": "Hex keys and calipers",
                    "items": [{"label": "hex keys", "confidence": 0.9}],
                },
                label_error="Vision model is unavailable" if failure else None,
                site="alameda-garage",
                suggested_bin_code="AGR-023",
                all_sites=["alameda-garage", "oakland-fab-east"],
                proposed_contents="" if failure else "hex keys, calipers",
            )
        )

    app.mount(
        "/bins",
        create_catalog_app(
            port=port,
            base_path="/bins",
            authoring_enabled=True,
            authoring_base_path="",
            passport_loader=lambda: PASSPORTS,
            containment_loader=lambda: {},
            virtual_loader=lambda: [],
            label_drift_loader=lambda: PROPOSALS,
            photo_source=_SyntheticPhotos(),
        ),
    )
    app.mount(
        "/",
        create_authoring_app(
            port=port,
            manage_loader=_manage_view,
            saved_photo_source=_SyntheticSavedPhotos(),
        ),
    )
    return app


if __name__ == "__main__":
    os.environ.pop("BINKEEPER_DATABASE_URL", None)
    os.environ["BINKEEPER_WRITES_ENABLED"] = "0"
    port = int(sys.argv[1])
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, access_log=False)
