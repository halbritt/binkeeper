"""Path helpers for mounted operator web surfaces."""

from __future__ import annotations


def normalize_base_path(base_path: str | None) -> str:
    """Return a normalized mount prefix with no trailing slash."""
    if base_path is None:
        return ""
    normalized = base_path.strip()
    if normalized in {"", "/"}:
        return ""
    if "://" in normalized:
        raise ValueError("base_path must be a local path prefix, not an absolute URL")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized.rstrip("/")


def static_path(base_path: str | None, asset: str, *, version: str | None = None) -> str:
    """Return the mount-aware URL of a shared static asset.

    Every shared shell asset carries the ``?v=<package version>`` cache-busting
    query (RFC 0081); freshness within a version is handled by the
    ``Cache-Control: no-cache`` serving policy of the shared static mount.
    """
    asset_url = surface_path(base_path, f"/shared-static/{asset}")
    if version:
        return f"{asset_url}?v={version}"
    return asset_url


def surface_path(base_path: str | None, path: str = "/") -> str:
    """Join a normalized web-surface prefix and an absolute local path."""
    prefix = normalize_base_path(base_path)
    if path == "":
        path = "/"
    if not path.startswith("/"):
        path = f"/{path}"
    if prefix == "":
        return path
    if path == "/":
        return f"{prefix}/"
    return f"{prefix}{path}"
