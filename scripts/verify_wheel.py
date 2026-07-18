"""Verify the standalone wheel contains both template resource roots."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED_RESOURCES = {
    "binkeeper/catalog/templates/README.txt",
    "binkeeper/photo/templates/README.txt",
}


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify_wheel.py PATH_TO_WHEEL")
    wheel = Path(sys.argv[1])
    with zipfile.ZipFile(wheel) as archive:
        missing = REQUIRED_RESOURCES.difference(archive.namelist())
    if missing:
        raise SystemExit(f"wheel is missing package resources: {', '.join(sorted(missing))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
