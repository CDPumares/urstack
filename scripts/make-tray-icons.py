#!/usr/bin/env python3
"""Generate the greyscale tray icon set from the colour app icon.

Tray icons sit next to system indicators, so UrStack ships a desaturated variant
rather than the colour logo. Regenerate with:

    python3 scripts/make-tray-icons.py

Source icons are data/icons/hicolor/<size>/apps/urstack.png; each one gets a
sibling urstack-tray.png.
"""

from __future__ import annotations

import sys
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HICOLOR = ROOT / "data" / "icons" / "hicolor"
SOURCE_NAME = "urstack.png"
TRAY_NAME = "urstack-tray.png"


def to_grey(src: Path, dest: Path) -> bool:
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file(str(src))
    except GLib.Error as exc:
        print(f"  ! {src}: {exc}", file=sys.stderr)
        return False
    if pb is None:
        return False
    if not pb.get_has_alpha():
        pb = pb.add_alpha(False, 0, 0, 0)
    out = pb.copy()
    # saturation 0 keeps the artwork's shading and alpha intact while dropping
    # colour, which a hand-rolled luminance pass over the raw buffer would not.
    pb.saturate_and_pixelate(out, 0.0, False)
    try:
        out.savev(str(dest), "png", [], [])
    except GLib.Error as exc:
        print(f"  ! {dest}: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    if not HICOLOR.is_dir():
        print(f"missing {HICOLOR}", file=sys.stderr)
        return 1
    made = 0
    for src in sorted(HICOLOR.glob(f"*/apps/{SOURCE_NAME}")):
        dest = src.with_name(TRAY_NAME)
        if to_grey(src, dest):
            print(f"  {dest.relative_to(ROOT)}")
            made += 1
    if not made:
        print("no icons generated", file=sys.stderr)
        return 1
    print(f"{made} tray icons generated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
