"""Standalone loopback service composition and readiness boundary."""

from __future__ import annotations

import argparse
import os
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Final

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from binkeeper.backup import BackupError, backup_key_from_config, check_backup_freshness
from binkeeper.bin_catalog_web import PassportLoader
from binkeeper.bin_catalog_web import create_app as create_catalog_app
from binkeeper.bin_photo_web import create_app as create_authoring_app
from binkeeper.blob_vault import BlobVaultError, load_vault_config
from binkeeper.database import ServingRoleUnavailableError, connect
from binkeeper.write_authority import writes_enabled as writer_authority_enabled

FROZEN_HOST: Final[str] = "127.0.0.1"
FROZEN_PORT: Final[int] = 8766
FROZEN_TAILNET_HOST: Final[str] = "proximal.tail0ecc2e.ts.net"
FROZEN_TAILNET_ORIGIN: Final[str] = f"https://{FROZEN_TAILNET_HOST}:{FROZEN_PORT}"

ReadinessProbe = Callable[[], tuple[bool, str]]


def database_readiness() -> tuple[bool, str]:
    """Check the least-privilege database path without mutating evidence."""
    try:
        with connect(role="serving") as conn:
            row = conn.execute("SELECT 1").fetchone()
    except (ServingRoleUnavailableError, OSError, RuntimeError, psycopg.Error) as exc:
        return False, f"database unavailable: {type(exc).__name__}"
    return (True, "ready") if row == (1,) else (False, "database probe returned no row")


def backup_readiness() -> tuple[bool, str]:
    """Require an authenticated backup inside the accepted age before cutover."""
    try:
        config = load_vault_config()
        result = check_backup_freshness(
            Path(os.environ.get("BINKEEPER_BACKUP_ROOT", "/var/lib/binkeeper/backups")),
            backup_key=backup_key_from_config(config),
            max_age=timedelta(
                seconds=int(os.environ.get("BINKEEPER_BACKUP_MAX_AGE_SECONDS", "90000"))
            ),
        )
    except (BackupError, BlobVaultError, OSError, ValueError) as exc:
        return False, f"backup unavailable: {type(exc).__name__}"
    return True, f"backup age {int(result.age.total_seconds())} seconds"


def operational_readiness(
    *,
    database_probe: ReadinessProbe = database_readiness,
    durability_probe: ReadinessProbe = backup_readiness,
) -> tuple[bool, str]:
    """Require both the serving database path and recent recoverability proof."""
    database_ready, database_detail = database_probe()
    if not database_ready:
        return False, database_detail
    durability_ready, durability_detail = durability_probe()
    if not durability_ready:
        return False, durability_detail
    return True, f"ready; {durability_detail}"


def create_app(
    *,
    readiness_probe: ReadinessProbe = operational_readiness,
    passport_loader: PassportLoader | None = None,
    writes_enabled: bool | None = None,
) -> FastAPI:
    """Compose health, catalog, media, and reviewed authoring on one loopback port."""
    paired = (("https", FROZEN_TAILNET_HOST),)
    writer_open = (
        writes_enabled
        if writes_enabled is not None
        else writer_authority_enabled()
    )
    app = FastAPI(title="BinKeeper", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def require_writer_authority(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not writer_open:
            return JSONResponse(
                {"status": "unavailable", "detail": "BinKeeper writer is frozen"},
                status_code=503,
            )
        return await call_next(request)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready() -> object:
        is_ready, detail = readiness_probe()
        payload = {"status": "ready" if is_ready else "unavailable", "detail": detail}
        return payload if is_ready else JSONResponse(payload, status_code=503)

    catalog = create_catalog_app(
        host=FROZEN_HOST,
        port=FROZEN_PORT,
        base_path="/bins",
        allowed_paired_origins=paired,
        authoring_enabled=writer_open,
        passport_loader=passport_loader,
    )
    authoring = create_authoring_app(
        host=FROZEN_HOST,
        port=FROZEN_PORT,
        allowed_paired_origins=paired,
    )
    app.mount("/bins", catalog)
    app.mount("/", authoring)
    return app


app = create_app()


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="binkeeper-serve")
    parser.add_argument("--host", default=os.environ.get("BINKEEPER_HOST", FROZEN_HOST))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("BINKEEPER_PORT", FROZEN_PORT))
    )
    args = parser.parse_args(argv)
    if args.host != FROZEN_HOST or args.port != FROZEN_PORT:
        parser.error(f"standalone service is frozen to {FROZEN_HOST}:{FROZEN_PORT}")
    import uvicorn

    uvicorn.run("binkeeper.service:app", host=args.host, port=args.port, access_log=False)
