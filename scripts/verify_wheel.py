"""Verify the standalone wheel contains every owner-workflow resource."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED_RESOURCES = {
    "binkeeper/bin_catalog_web/templates/bin_catalog.html",
    "binkeeper/bin_photo_web/templates/bin_manage.html",
    "binkeeper/bin_photo_web/templates/bin_photo_base.html",
    "binkeeper/bin_photo_web/templates/bin_photo_form.html",
    "binkeeper/bin_photo_web/templates/bin_photo_result.html",
    "binkeeper/bin_photo_web/templates/bin_register_done.html",
    "binkeeper/bin_photo_web/templates/bin_register_form.html",
    "binkeeper/bin_photo_web/templates/bin_register_pick.html",
    "binkeeper/catalog/templates/README.txt",
    "binkeeper/photo/templates/README.txt",
    "binkeeper/web/static/binkeeper.css",
    "binkeeper/web/static/keyboard.js",
    "binkeeper/web/templates/_app_shell.html",
    "binkeeper/web/templates/_audit_footer.html",
    "binkeeper/web/templates/_binkeeper_tabs.html",
    "binkeeper/web/templates/_cli_command_card.html",
    "binkeeper/web/templates/_components.html",
    "binkeeper/web/templates/_error_banner.html",
    "binkeeper/web/templates/_error_page.html",
    "binkeeper/web/templates/_future_slot.html",
    "binkeeper/web/templates/_help_modal.html",
    "binkeeper/web/templates/_status_banner.html",
    "binkeeper/web/templates/_status_chip.html",
    "binkeeper/web/templates/_surface_tabs.html",
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
