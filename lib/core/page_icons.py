"""Page icon names and SVG-as-symbolic detection — no GTK required.

Kept separate from ui.py so unit tests (and GitHub's setup-python) can check
icon policy without importing PyGObject.
"""

from __future__ import annotations

import re

# Page icons: first name that exists in the current theme wins.
# Prefer outline action/place/device symbols so sidebar, heroes, and cards match.
PAGE_ICON_CANDIDATES: dict[str, tuple[str, ...]] = {
    "overview": ("user-home-symbolic", "go-home-symbolic"),
    "home": ("system-software-update-symbolic", "software-update-available-symbolic"),
    "apps": (
        "applications-all-symbolic",
        "view-app-grid-symbolic",
        "applications-other-symbolic",
    ),
    "health": ("computer-symbolic", "applications-system-symbolic"),
    "look": (
        "image-x-generic-symbolic",
        "folder-pictures-symbolic",
        "camera-photo-symbolic",
        "urstack-look-symbolic",
        "preferences-desktop-wallpaper-symbolic",
        "applications-graphics-symbolic",
    ),
    "backup": ("document-save-symbolic",),
    "restore": ("document-revert-symbolic", "edit-undo-symbolic"),
    "settings": ("configure-symbolic", "preferences-system-symbolic"),
    "log": ("document-open-recent-symbolic",),
    "runs": ("folder-documents-symbolic", "folder-symbolic"),
    "close": ("window-close-symbolic", "application-exit-symbolic"),
}

_SVG_FILL_RE = re.compile(
    r"""(?:fill\s*=\s*["']([^"']+)["']|fill\s*:\s*([^;}]+))""",
    re.IGNORECASE,
)


def svg_reads_as_symbolic(svg: str) -> bool:
    """True if this SVG is a monochrome/symbolic icon, not a full-color drawing.

    Breeze sometimes ships full-color panel artwork under a ``*-symbolic`` name.
    Those must not win over a real outline icon later in the candidate list.
    """
    if not svg:
        return True
    if "currentColor" in svg or "ColorScheme-Text" in svg:
        return True
    fills: set[str] = set()
    for match in _SVG_FILL_RE.finditer(svg):
        val = (match.group(1) or match.group(2) or "").strip().lower()
        if not val or val in {"none", "currentcolor", "transparent"}:
            continue
        fills.add(val)
    return len(fills) <= 1
