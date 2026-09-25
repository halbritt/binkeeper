"""Serve synthetic BinKeeper pages for local browser checks, without a database."""

from __future__ import annotations

import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from io import BytesIO

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from PIL import Image, ImageDraw
from starlette.responses import Response

from binkeeper.bin_catalog_web import create_app as create_catalog_app
from binkeeper.bin_label_drift import LabelDriftQueueEntry
from binkeeper.bin_passport import BinPassport
from binkeeper.bin_photo_web import create_app as create_authoring_app
from binkeeper.bin_photo_web.manage import ManageView


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
    proposed_theme="Measuring tools",
    current_theme="Precision tools",
    current_contents="hex keys and digital calipers",
    new_item_labels=("tape measure",),
    photo_hashes=(),
    model_versions=("synthetic-model",),
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
            label_drift_loader=lambda: [PROPOSAL],
            photo_source=_SyntheticPhotos(),
        ),
    )
    app.mount("/", create_authoring_app(port=port, manage_loader=_manage_view))
    return app


if __name__ == "__main__":
    os.environ.pop("BINKEEPER_DATABASE_URL", None)
    os.environ["BINKEEPER_WRITES_ENABLED"] = "0"
    port = int(sys.argv[1])
    uvicorn.run(create_app(port), host="127.0.0.1", port=port, access_log=False)
