"""Package-local template/static helpers for BinKeeper web surfaces."""

from __future__ import annotations

from collections.abc import Iterator
from importlib import resources
from pathlib import Path

RESOURCE_DIRS: tuple[str, ...] = ("templates", "static")

EXTERNAL_ASSET_MARKERS: tuple[str, ...] = (
    "http://",
    "https://",
    "//unpkg.com",
    "unpkg.com",
    "cdn.jsdelivr.net",
    "cdnjs.cloudflare.com",
    "googleapis.com",
    "googletagmanager.com",
    "@import",
    "url(http://",
    "url(https://",
    'src="//',
    'href="//',
)


def resource_dir(name: str) -> Path:
    """Return a package-local resource directory path."""
    if name not in RESOURCE_DIRS:
        raise ValueError(f"unknown web resource directory: {name!r}")
    return Path(str(resources.files("binkeeper.web") / name))


def template_dir() -> Path:
    """Return the shared Jinja template directory."""
    return resource_dir("templates")


def static_dir() -> Path:
    """Return the shared static asset directory."""
    return resource_dir("static")


def find_external_asset_references(text: str) -> tuple[str, ...]:
    """Return external asset markers found in rendered HTML/CSS/JS text."""
    lower_text = text.lower()
    return tuple(marker for marker in EXTERNAL_ASSET_MARKERS if marker in lower_text)


def assert_no_external_asset_references(text: str) -> None:
    """Raise if rendered text contains an external asset marker."""
    markers = find_external_asset_references(text)
    if markers:
        joined = ", ".join(markers)
        raise ValueError(f"external asset reference(s) found: {joined}")


def iter_shared_resource_texts() -> Iterator[tuple[str, str]]:
    """Yield package-local shared web resource text for no-CDN checks."""
    for directory in RESOURCE_DIRS:
        root = resources.files("binkeeper.web") / directory
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_file():
                yield f"{directory}/{child.name}", child.read_text(encoding="utf-8")
