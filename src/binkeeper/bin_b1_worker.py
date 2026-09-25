"""Bounded child process for the optional local Niimbot B1 BLE driver."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", required=True)
    parser.add_argument("--copies", type=int, choices=(1, 2), required=True)
    parser.add_argument("--image", type=Path, required=True)
    args = parser.parse_args()

    try:
        from niimbot_b1 import NiimbotB1Printer
    except ImportError:
        print("Install BinKeeper with its b1 extra to enable B1 printing", file=sys.stderr)
        return 2

    with Image.open(args.image) as image:
        printer = NiimbotB1Printer(args.address)
        try:
            printer.connect()
            if not printer.print_image(image, copies=args.copies):
                print("B1 did not confirm print completion; check the printer", file=sys.stderr)
                return 3
        finally:
            printer.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
