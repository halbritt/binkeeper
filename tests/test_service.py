from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from binkeeper.service import (
    FROZEN_PORT,
    FROZEN_TAILNET_ORIGIN,
    create_app,
    operational_readiness,
)


def test_frozen_owner_endpoint_is_collision_free_port() -> None:
    assert FROZEN_PORT == 8766
    assert FROZEN_TAILNET_ORIGIN == "https://proximal.tail0ecc2e.ts.net:8766"


def test_health_and_readiness_are_distinct() -> None:
    healthy = TestClient(create_app(readiness_probe=lambda: (True, "ready")))
    unavailable = TestClient(create_app(readiness_probe=lambda: (False, "database unavailable")))

    assert healthy.get("/healthz").json() == {"status": "ok"}
    assert healthy.get("/readyz").json() == {"status": "ready", "detail": "ready"}
    response = unavailable.get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "detail": "database unavailable"}


def test_operational_readiness_requires_database_and_recent_backup() -> None:
    assert operational_readiness(
        database_probe=lambda: (False, "database unavailable"),
        durability_probe=lambda: pytest.fail("backup probe must not run"),
    ) == (False, "database unavailable")
    assert operational_readiness(
        database_probe=lambda: (True, "ready"),
        durability_probe=lambda: (False, "backup stale"),
    ) == (False, "backup stale")
    assert operational_readiness(
        database_probe=lambda: (True, "ready"),
        durability_probe=lambda: (True, "backup age 60 seconds"),
    ) == (True, "ready; backup age 60 seconds")


def test_catalog_and_authoring_are_mounted_without_hosted_assets() -> None:
    app = create_app(
        readiness_probe=lambda: (True, "ready"),
        passport_loader=lambda: [],
    )
    client = TestClient(app, base_url="http://127.0.0.1:8766")

    assert client.get("/bins/").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/shared-static/binkeeper.css").status_code == 200
