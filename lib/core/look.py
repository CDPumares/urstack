#!/usr/bin/env python3
"""Desktop look packs: export the live theme, and install a theme archive.

A look pack is a tar (optionally gzip/xz/bzip2) or zip that carries the
wallpaper, icon/cursor/GTK/Plasma assets, and the settings that select them.
Install is user-local only (~/.local/share, ~/.themes, ~/.icons, ~/.config).
Archives never run scripts, and members that leave the destination are refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from xml.etree import ElementTree

FORMAT = "urstack-look"
FORMAT_VERSION = 1
HOME_TOKEN = "__URSTACK_HOME__"

DESKTOP_LABELS = {
    "plasma": "KDE Plasma",
    "gnome": "GNOME",
    "cinnamon": "Cinnamon",
    "xfce": "XFCE",
    "cosmic": "COSMIC",
    "mate": "MATE",
    "lxqt": "LXQt",
    "budgie": "Budgie",
    "unknown": "This desktop",
}

# Names we record but do not copy out of /usr (they ship with Fedora).
STOCK_ICON_THEMES = {
    "hicolor",
    "locolor",
    "default",
    "Adwaita",
    "Adwaita-dark",
    "HighContrast",
    "breeze",
    "breeze-dark",
    "Breeze",
    "gnome",
    "oxygen",
}
STOCK_CURSOR_THEMES = {
    "default",
    "Adwaita",
    "breeze_cursors",
    "Breeze",
    "KDE_Classic",
    "breeze-dark",
}
STOCK_GTK_THEMES = {
    "Adwaita",
    "Adwaita-dark",
    "Adwaita-HighContrast",
    "Breeze",
    "Breeze-Dark",
    "HighContrast",
    "Default",
    "Emacs",
}
STOCK_LOOKANDFEEL = {
    "org.kde.breeze.desktop",
    "org.kde.breezedark.desktop",
    "org.kde.breezetwilight.desktop",
}

INCLUDE_KEYS = (
    "wallpaper",
    "icons",
    "cursors",
    "gtk",
    "colors",
    "widgets",
    "fonts",
    "layout",
)

_SKIP_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "Cache",
    "cache",
}
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".jxl", ".avif", ".svg"}
_SAFE_ARCHIVE_SUFFIXES = {
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.xz",
    ".txz",
    ".tar.bz2",
    ".tbz2",
    ".zip",
}

_KDE_IMAGE_RE = re.compile(
    r"^(Image|PreviewImage|customButtonImage|usersWallpapers|SlidePaths)=(.*)$"
)
_PLUGIN_RE = re.compile(r"^plugin=(.+)$", re.IGNORECASE)


class LookError(Exception):
    """User-facing failure (unsafe archive, missing file, refused path)."""


# ---------------------------------------------------------------------------
# Desktop detection and small helpers
# ---------------------------------------------------------------------------
def detect_desktop(environ: dict[str, str] | None = None) -> str:
    env = environ if environ is not None else os.environ
    raw = (env.get("XDG_CURRENT_DESKTOP") or env.get("DESKTOP_SESSION") or "").lower()
    parts = {p.strip() for p in raw.replace(":", ";").split(";") if p.strip()}
    if parts & {"kde", "plasma", "plasmawayland"}:
        return "plasma"
    if "cinnamon" in parts or "x-cinnamon" in parts:
        return "cinnamon"
    if "xfce" in parts or "xfce4" in parts:
        return "xfce"
    if "cosmic" in parts:
        return "cosmic"
    if "mate" in parts:
        return "mate"
    if "lxqt" in parts:
        return "lxqt"
    if "budgie" in parts:
        return "budgie"
    if "gnome" in parts or "ubuntu:gnome" in raw:
        return "gnome"
    session = (env.get("DESKTOP_SESSION") or "").lower()
    for key in ("plasma", "gnome", "cinnamon", "xfce", "cosmic", "mate", "lxqt", "budgie"):
        if key in session:
            return key
    return "unknown"


def desktop_label(desktop: str) -> str:
    return DESKTOP_LABELS.get(desktop, DESKTOP_LABELS["unknown"])


def _progress(msg: str, pct: int | None = None) -> None:
    if msg:
        print(f"# {msg}", flush=True)
    if pct is not None:
        print(str(max(0, min(100, int(pct)))), flush=True)


def _run(argv: list[str], *, timeout: int = 8) -> str:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return (proc.stdout or "").strip()


def _gsettings_get(schema: str, key: str) -> str:
    raw = _run(["gsettings", "get", schema, key])
    if not raw or raw in {"''", '""', "@ms nothing", "nothing"}:
        return ""
    if (raw.startswith("'") and raw.endswith("'")) or (
        raw.startswith('"') and raw.endswith('"')
    ):
        raw = raw[1:-1]
    return raw.replace("\\'", "'")


def _gsettings_set(schema: str, key: str, value: str) -> bool:
    if not shutil.which("gsettings"):
        return False
    proc = subprocess.run(
        ["gsettings", "set", schema, key, value],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    return proc.returncode == 0


def _strip_file_uri(value: str) -> str:
    value = (value or "").strip().strip("'\"")
    if value.startswith("file://"):
        value = value[7:]
    return value


def _kde_get(path: Path, section: str, key: str, default: str = "") -> str:
    if not path.is_file():
        return default
    current = ""
    want = section.strip("[]")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped.strip("[]")
            continue
        if current != want or "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        if k.strip() == key:
            return v.strip()
    return default


def _parse_kv_file(path: Path) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = {}
    if not path.is_file():
        return data
    section = ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.strip("[]")
            data.setdefault(section, {})
            continue
        if "=" not in stripped:
            continue
        k, _, v = stripped.partition("=")
        data.setdefault(section, {})[k.strip()] = v.strip()
    return data


def _iter_kv_matches(path: Path, key_re: re.Pattern[str]) -> Iterator[tuple[str, str]]:
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in text.splitlines():
        m = key_re.match(line.strip())
        if m:
            yield m.group(1), m.group(2)


def _which_theme_dir(name: str, roots: Iterable[Path]) -> Path | None:
    if not name or name in {".", ".."}:
        return None
    for root in roots:
        candidate = root / name
        if candidate.is_dir():
            return candidate
    return None


def _icon_roots(home: Path) -> list[Path]:
    return [
        home / ".local/share/icons",
        home / ".icons",
        Path("/usr/share/icons"),
        Path("/usr/local/share/icons"),
    ]


def _gtk_roots(home: Path) -> list[Path]:
    return [
        home / ".local/share/themes",
        home / ".themes",
        Path("/usr/share/themes"),
        Path("/usr/local/share/themes"),
    ]


def _color_roots(home: Path) -> list[Path]:
    return [
        home / ".local/share/color-schemes",
        Path("/usr/share/color-schemes"),
    ]


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _copy_file(src: Path, dest: Path) -> bool:
    if not src.is_file() or src.is_symlink():
        if src.is_symlink():
            try:
                target = src.resolve()
            except OSError:
                return False
            if not target.is_file():
                return False
            src = target
        elif not src.is_file():
            return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(src, dest, follow_symlinks=True)
        return True
    except OSError:
        return False


def _copy_tree(src: Path, dest: Path, *, limit: int = 80_000) -> int:
    """Copy a directory. Skip caches and outbound symlinks. Return file count."""
    if not src.is_dir():
        return 0
    copied = 0
    src_res = src.resolve()
    for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIR_NAMES]
        here = Path(dirpath)
        for name in list(dirnames):
            child = here / name
            if child.is_symlink():
                dirnames.remove(name)
        rel = here.relative_to(src)
        target_dir = dest if str(rel) == "." else dest / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            if copied >= limit:
                return copied
            src_f = here / name
            if src_f.is_symlink():
                try:
                    resolved = src_f.resolve()
                except OSError:
                    continue
                if not _is_under(resolved, src_res) and not resolved.is_file():
                    continue
                if not resolved.is_file():
                    continue
                src_f = resolved
            if not src_f.is_file():
                continue
            dest_f = target_dir / name
            try:
                shutil.copy2(src_f, dest_f, follow_symlinks=True)
                copied += 1
            except OSError:
                continue
    return copied


def _tokenise_home(text: str, home: Path) -> str:
    home_s = str(home)
    return text.replace(home_s, HOME_TOKEN)


def _untokenise_home(text: str, home: Path) -> str:
    return text.replace(HOME_TOKEN, str(home))


def _safe_rel(name: str) -> Path | None:
    if not name or name.startswith("/") or name.startswith("\\"):
        return None
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return None
    if p.parts and p.parts[0] == "~":
        return None
    return p


# ---------------------------------------------------------------------------
# Inspect the live session
# ---------------------------------------------------------------------------
@dataclass
class LookItem:
    id: str
    title: str
    value: str
    bundled: bool = False
    paths: list[str] = field(default_factory=list)
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "value": self.value,
            "bundled": self.bundled,
            "paths": self.paths,
            "note": self.note,
        }


@dataclass
class LookSnapshot:
    desktop: str
    items: list[LookItem] = field(default_factory=list)
    wallpaper_files: list[Path] = field(default_factory=list)
    icon_name: str = ""
    icon_dir: Path | None = None
    cursor_name: str = ""
    cursor_dir: Path | None = None
    gtk_name: str = ""
    gtk_dir: Path | None = None
    color_scheme: str = ""
    color_file: Path | None = None
    lookandfeel: str = ""
    lookandfeel_dir: Path | None = None
    widget_ids: list[str] = field(default_factory=list)
    widget_dirs: list[Path] = field(default_factory=list)
    font_names: list[str] = field(default_factory=list)
    font_files: list[Path] = field(default_factory=list)
    config_files: list[Path] = field(default_factory=list)
    extra_trees: dict[str, Path] = field(default_factory=dict)

    def summary(self) -> str:
        bits = [desktop_label(self.desktop)]
        if self.gtk_name:
            bits.append(self.gtk_name)
        elif self.color_scheme:
            bits.append(self.color_scheme)
        if self.icon_name:
            bits.append(self.icon_name)
        wall = self.wallpaper_files[0].name if self.wallpaper_files else ""
        if wall:
            bits.append(wall)
        return " · ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        preview = str(self.wallpaper_files[0]) if self.wallpaper_files else ""
        return {
            "desktop": self.desktop,
            "desktop_label": desktop_label(self.desktop),
            "summary": self.summary(),
            "preview": preview,
            "items": [it.as_dict() for it in self.items],
        }


def _wallpaper_from_kde(home: Path) -> list[Path]:
    found: list[Path] = []
    for rel in (
        ".config/plasma-org.kde.plasma.desktop-appletsrc",
        ".config/kscreenlockerrc",
    ):
        path = home / rel
        for _key, raw in _iter_kv_matches(path, _KDE_IMAGE_RE):
            for part in raw.split(","):
                p = Path(_strip_file_uri(part.strip()))
                if p.is_file():
                    found.append(p)
    extra = home / ".local/share/wallpapers"
    if extra.is_dir() and not found:
        for path in extra.rglob("*"):
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                found.append(path)
    return list(dict.fromkeys(found))


def _wallpaper_from_gsettings(schema: str, *keys: str) -> list[Path]:
    found: list[Path] = []
    for key in keys:
        p = Path(_strip_file_uri(_gsettings_get(schema, key)))
        if p.is_file():
            found.append(p)
    return found


def _wallpaper_from_xfce(home: Path) -> list[Path]:
    xml = home / ".config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml"
    if not xml.is_file():
        return []
    found: list[Path] = []
    try:
        tree = ElementTree.parse(xml)
    except (ElementTree.ParseError, OSError):
        return []
    for el in tree.iter():
        name = el.attrib.get("name", "")
        if name in {"last-image", "image-path"} or name.endswith("/last-image"):
            p = Path(_strip_file_uri(el.attrib.get("value", "")))
            if p.is_file():
                found.append(p)
    return list(dict.fromkeys(found))


def _wallpaper_from_cosmic(home: Path) -> list[Path]:
    root = home / ".config/cosmic"
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            found.append(path)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"(?:file://)?(/[^\s\"']+\.(?:png|jpe?g|webp|jxl|avif))", text):
            p = Path(m.group(1))
            if p.is_file():
                found.append(p)
    return list(dict.fromkeys(found))


def _plasma_widgets(home: Path) -> tuple[list[str], list[Path]]:
    applets = home / ".config/plasma-org.kde.plasma.desktop-appletsrc"
    ids: list[str] = []
    if applets.is_file():
        try:
            text = applets.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            m = _PLUGIN_RE.match(line.strip())
            if not m:
                continue
            pid = m.group(1).strip()
            if pid and pid not in ids:
                ids.append(pid)
    dirs: list[Path] = []
    for pid in ids:
        for root in (
            home / ".local/share/plasma/plasmoids",
            home / ".local/share/plasma/look-and-feel",
        ):
            candidate = root / pid
            if candidate.is_dir():
                dirs.append(candidate)
    return ids, dirs


def _user_fonts_for(names: list[str], home: Path) -> list[Path]:
    font_root = home / ".local/share/fonts"
    if not names or not font_root.is_dir():
        return []
    lowered = {n.split(",")[0].strip().lower() for n in names if n.strip()}
    found: list[Path] = []
    for path in font_root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".ttf", ".otf", ".ttc", ".woff", ".woff2"}:
            continue
        stem = path.stem.lower().replace("-", " ").replace("_", " ")
        if any(n and n in stem for n in lowered):
            found.append(path)
    return found


def inspect_look(
    home: Path | None = None,
    environ: dict[str, str] | None = None,
) -> LookSnapshot:
    home = Path(home) if home is not None else Path.home()
    desktop = detect_desktop(environ)
    snap = LookSnapshot(desktop=desktop)
    kdeglobals = home / ".config/kdeglobals"

    snap.icon_name = (
        _kde_get(kdeglobals, "Icons", "Theme")
        or _gsettings_get("org.gnome.desktop.interface", "icon-theme")
        or _gsettings_get("org.cinnamon.desktop.interface", "icon-theme")
        or _gsettings_get("org.mate.interface", "icon-theme")
    )
    snap.cursor_name = (
        _kde_get(home / ".config/kcminputrc", "Mouse", "cursorTheme")
        or _gsettings_get("org.gnome.desktop.interface", "cursor-theme")
        or _gsettings_get("org.cinnamon.desktop.interface", "cursor-theme")
    )
    snap.gtk_name = (
        _kde_get(home / ".config/gtk-3.0/settings.ini", "Settings", "gtk-theme-name")
        or _gsettings_get("org.gnome.desktop.interface", "gtk-theme")
        or _gsettings_get("org.gnome.desktop.interface", "color-scheme")
        or _gsettings_get("org.cinnamon.desktop.interface", "gtk-theme")
        or _gsettings_get("org.mate.interface", "gtk-theme")
    )
    if snap.gtk_name.startswith("prefer-"):
        # GNOME color-scheme, not a GTK theme name.
        color_pref = snap.gtk_name
        snap.gtk_name = _gsettings_get("org.gnome.desktop.interface", "gtk-theme")
    else:
        color_pref = _gsettings_get("org.gnome.desktop.interface", "color-scheme")
    snap.color_scheme = _kde_get(kdeglobals, "General", "ColorScheme") or color_pref
    snap.lookandfeel = _kde_get(kdeglobals, "KDE", "LookAndFeelPackage")

    if desktop == "plasma":
        snap.wallpaper_files = _wallpaper_from_kde(home)
    elif desktop == "gnome" or desktop == "budgie":
        snap.wallpaper_files = _wallpaper_from_gsettings(
            "org.gnome.desktop.background", "picture-uri", "picture-uri-dark"
        )
    elif desktop == "cinnamon":
        snap.wallpaper_files = _wallpaper_from_gsettings(
            "org.cinnamon.desktop.background", "picture-uri"
        )
    elif desktop == "mate":
        snap.wallpaper_files = _wallpaper_from_gsettings(
            "org.mate.background", "picture-filename"
        )
    elif desktop == "xfce":
        snap.wallpaper_files = _wallpaper_from_xfce(home)
    elif desktop == "cosmic":
        snap.wallpaper_files = _wallpaper_from_cosmic(home)
    else:
        snap.wallpaper_files = (
            _wallpaper_from_kde(home)
            or _wallpaper_from_gsettings(
                "org.gnome.desktop.background", "picture-uri", "picture-uri-dark"
            )
            or _wallpaper_from_xfce(home)
        )

    snap.icon_dir = _which_theme_dir(snap.icon_name, _icon_roots(home))
    snap.cursor_dir = _which_theme_dir(snap.cursor_name, _icon_roots(home))
    snap.gtk_dir = _which_theme_dir(snap.gtk_name, _gtk_roots(home))
    if snap.color_scheme:
        for root in _color_roots(home):
            candidate = root / f"{snap.color_scheme}.colors"
            if candidate.is_file():
                snap.color_file = candidate
                break
    if snap.lookandfeel:
        for root in (
            home / ".local/share/plasma/look-and-feel",
            Path("/usr/share/plasma/look-and-feel"),
        ):
            candidate = root / snap.lookandfeel
            if candidate.is_dir():
                snap.lookandfeel_dir = candidate
                break

    if desktop == "plasma":
        snap.widget_ids, snap.widget_dirs = _plasma_widgets(home)

    fonts = [
        _kde_get(kdeglobals, "General", "font"),
        _kde_get(kdeglobals, "General", "menuFont"),
        _kde_get(kdeglobals, "WM", "activeFont"),
        _gsettings_get("org.gnome.desktop.interface", "font-name"),
        _gsettings_get("org.gnome.desktop.interface", "document-font-name"),
        _gsettings_get("org.gnome.desktop.interface", "monospace-font-name"),
    ]
    snap.font_names = [f for f in fonts if f]
    snap.font_files = _user_fonts_for(snap.font_names, home)

    configs: list[Path] = []
    if desktop in {"plasma", "unknown"}:
        configs += [
            home / ".config/kdeglobals",
            home / ".config/kwinrc",
            home / ".config/kcminputrc",
            home / ".config/kscreenlockerrc",
            home / ".config/ksplashrc",
            home / ".config/breezerc",
            home / ".config/plasmashellrc",
            home / ".config/plasmarc",
            home / ".config/plasma-org.kde.plasma.desktop-appletsrc",
            home / ".config/gtk-3.0/settings.ini",
            home / ".config/gtk-4.0/settings.ini",
            home / ".config/kdedefaults/kdeglobals",
            home / ".config/Kvantum/kvantum.kvconfig",
        ]
    if desktop in {"gnome", "budgie", "cinnamon", "mate", "unknown"}:
        configs += [
            home / ".config/gtk-3.0/settings.ini",
            home / ".config/gtk-4.0/settings.ini",
            home / ".gtkrc-2.0",
        ]
    if desktop == "xfce":
        xfce_xml = home / ".config/xfce4/xfconf/xfce-perchannel-xml"
        if xfce_xml.is_dir():
            configs += list(xfce_xml.glob("*.xml"))
    if desktop == "lxqt":
        configs += [
            home / ".config/lxqt/lxqt.conf",
            home / ".config/lxqt/session.conf",
            home / ".config/lxqt/panel.conf",
        ]
    snap.config_files = [p for p in configs if p.is_file()]

    if (home / ".config/Kvantum").is_dir():
        snap.extra_trees["kvantum"] = home / ".config/Kvantum"
    if (home / ".config/cosmic").is_dir() and desktop in {"cosmic", "unknown"}:
        snap.extra_trees["cosmic"] = home / ".config/cosmic"
    if (home / ".local/share/aurorae").is_dir() and desktop == "plasma":
        snap.extra_trees["aurorae"] = home / ".local/share/aurorae"
    if (home / ".local/share/plasma/desktoptheme").is_dir() and desktop == "plasma":
        snap.extra_trees["desktoptheme"] = home / ".local/share/plasma/desktoptheme"

    def _item(
        iid: str,
        title: str,
        value: str,
        *,
        bundled: bool = False,
        paths: list[Path] | None = None,
        note: str = "",
    ) -> LookItem:
        return LookItem(
            id=iid,
            title=title,
            value=value or "—",
            bundled=bundled,
            paths=[str(p) for p in (paths or [])],
            note=note,
        )

    wall_note = ""
    if snap.wallpaper_files:
        wall_note = f"{len(snap.wallpaper_files)} file" + (
            "s" if len(snap.wallpaper_files) != 1 else ""
        )
    snap.items = [
        _item(
            "wallpaper",
            "Wallpaper",
            snap.wallpaper_files[0].name if snap.wallpaper_files else "Not detected",
            bundled=bool(snap.wallpaper_files),
            paths=snap.wallpaper_files,
            note=wall_note,
        ),
        _item(
            "icons",
            "Icons",
            snap.icon_name or "System default",
            bundled=bool(
                snap.icon_dir and snap.icon_name not in STOCK_ICON_THEMES
            ),
            paths=[snap.icon_dir] if snap.icon_dir else [],
            note=""
            if not snap.icon_name
            else (
                "Will be packed"
                if snap.icon_name not in STOCK_ICON_THEMES
                else "Fedora ships this — recorded by name"
            ),
        ),
        _item(
            "cursors",
            "Cursors",
            snap.cursor_name or "System default",
            bundled=bool(
                snap.cursor_dir and snap.cursor_name not in STOCK_CURSOR_THEMES
            ),
            paths=[snap.cursor_dir] if snap.cursor_dir else [],
        ),
        _item(
            "gtk",
            "Application theme",
            snap.gtk_name or snap.lookandfeel or "System default",
            bundled=bool(snap.gtk_dir and snap.gtk_name not in STOCK_GTK_THEMES)
            or bool(
                snap.lookandfeel_dir and snap.lookandfeel not in STOCK_LOOKANDFEEL
            ),
            paths=[p for p in (snap.gtk_dir, snap.lookandfeel_dir) if p],
        ),
        _item(
            "colors",
            "Colors",
            snap.color_scheme or "System default",
            bundled=bool(snap.color_file and _is_under(snap.color_file, home)),
            paths=[snap.color_file] if snap.color_file else [],
        ),
        _item(
            "widgets",
            "Widgets & panels",
            (
                f"{len(snap.widget_ids)} on the desktop"
                if snap.widget_ids
                else ("Plasma layout" if desktop == "plasma" else "Session layout")
            ),
            bundled=bool(snap.widget_dirs) or desktop in {"plasma", "xfce", "lxqt", "cosmic"},
            paths=snap.widget_dirs,
            note=f"{len(snap.widget_dirs)} custom widget" + (
                "s" if len(snap.widget_dirs) != 1 else ""
            )
            if snap.widget_dirs
            else "",
        ),
        _item(
            "fonts",
            "Fonts",
            ", ".join(dict.fromkeys(n.split(",")[0].strip() for n in snap.font_names))
            or "System default",
            bundled=bool(snap.font_files),
            paths=snap.font_files,
            note=f"{len(snap.font_files)} user font file"
            + ("s" if len(snap.font_files) != 1 else "")
            if snap.font_files
            else "System fonts are not copied",
        ),
        _item(
            "layout",
            "Desktop settings",
            f"{len(snap.config_files)} config file"
            + ("s" if len(snap.config_files) != 1 else ""),
            bundled=bool(snap.config_files),
            paths=snap.config_files,
        ),
    ]
    return snap


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------
def _includes(raw: str | None) -> set[str]:
    if not raw:
        return set(INCLUDE_KEYS)
    wanted = {p.strip() for p in raw.split(",") if p.strip()}
    return {k for k in INCLUDE_KEYS if k in wanted} or set(INCLUDE_KEYS)


def export_look(
    dest: Path,
    *,
    home: Path | None = None,
    environ: dict[str, str] | None = None,
    include: str | None = None,
    name: str = "",
) -> Path:
    home = Path(home) if home is not None else Path.home()
    dest = Path(dest)
    if dest.suffix.lower() not in {".gz", ".xz", ".bz2", ".zip"} and dest.suffix.lower() != ".tar":
        if "".join(dest.suffixes[-2:]).lower() not in _SAFE_ARCHIVE_SUFFIXES:
            dest = dest.with_suffix(dest.suffix + ".tar.xz") if dest.suffix else Path(str(dest) + ".tar.xz")
    wanted = _includes(include)
    _progress("Reading the current look…", 5)
    snap = inspect_look(home, environ)
    staging = Path(tempfile.mkdtemp(prefix="urstack-look-"))
    try:
        files_copied = 0
        bundled: dict[str, Any] = {}

        def note(key: str, **extra: Any) -> None:
            bundled[key] = extra

        if "wallpaper" in wanted and snap.wallpaper_files:
            _progress("Packing wallpaper…", 15)
            wdir = staging / "wallpaper"
            wdir.mkdir()
            names: list[str] = []
            for src in snap.wallpaper_files:
                target = wdir / src.name
                if _copy_file(src, target):
                    files_copied += 1
                    names.append(src.name)
            note("wallpaper", files=names)

        if "icons" in wanted and snap.icon_dir and snap.icon_name not in STOCK_ICON_THEMES:
            _progress(f"Packing icons ({snap.icon_name})…", 30)
            files_copied += _copy_tree(snap.icon_dir, staging / "icons" / snap.icon_name)
            note("icons", name=snap.icon_name, bundled=True)
        elif snap.icon_name:
            note("icons", name=snap.icon_name, bundled=False)

        if "cursors" in wanted and snap.cursor_dir and snap.cursor_name not in STOCK_CURSOR_THEMES:
            _progress(f"Packing cursors ({snap.cursor_name})…", 40)
            files_copied += _copy_tree(snap.cursor_dir, staging / "cursors" / snap.cursor_name)
            note("cursors", name=snap.cursor_name, bundled=True)
        elif snap.cursor_name:
            note("cursors", name=snap.cursor_name, bundled=False)

        if "gtk" in wanted:
            if snap.gtk_dir and snap.gtk_name not in STOCK_GTK_THEMES:
                _progress(f"Packing GTK theme ({snap.gtk_name})…", 50)
                files_copied += _copy_tree(snap.gtk_dir, staging / "gtk-themes" / snap.gtk_name)
                note("gtk", name=snap.gtk_name, bundled=True)
            elif snap.gtk_name:
                note("gtk", name=snap.gtk_name, bundled=False)
            if (
                snap.lookandfeel_dir
                and snap.lookandfeel not in STOCK_LOOKANDFEEL
            ):
                files_copied += _copy_tree(
                    snap.lookandfeel_dir,
                    staging / "plasma" / "look-and-feel" / snap.lookandfeel,
                )
                note("lookandfeel", name=snap.lookandfeel, bundled=True)
            elif snap.lookandfeel:
                note("lookandfeel", name=snap.lookandfeel, bundled=False)

        if "colors" in wanted and snap.color_file:
            _progress("Packing color scheme…", 58)
            dest_c = staging / "plasma" / "color-schemes" / snap.color_file.name
            if _copy_file(snap.color_file, dest_c):
                files_copied += 1
            note("colors", name=snap.color_scheme, bundled=True)
        elif snap.color_scheme:
            note("colors", name=snap.color_scheme, bundled=False)

        if "widgets" in wanted:
            _progress("Packing widgets…", 68)
            wids = []
            for wdir in snap.widget_dirs:
                files_copied += _copy_tree(
                    wdir, staging / "plasma" / "plasmoids" / wdir.name
                )
                wids.append(wdir.name)
            note("widgets", ids=snap.widget_ids, bundled=wids)

        if "fonts" in wanted and snap.font_files:
            _progress("Packing user fonts…", 75)
            fdir = staging / "fonts"
            fnames = []
            for src in snap.font_files:
                if _copy_file(src, fdir / src.name):
                    files_copied += 1
                    fnames.append(src.name)
            note("fonts", files=fnames, names=snap.font_names)
        elif snap.font_names:
            note("fonts", names=snap.font_names, bundled=False)

        if "layout" in wanted:
            _progress("Packing desktop settings…", 82)
            cfg = staging / "config"
            rels = []
            for src in snap.config_files:
                try:
                    rel = src.relative_to(home)
                except ValueError:
                    rel = Path("misc") / src.name
                dest_f = cfg / rel
                if _copy_file(src, dest_f):
                    try:
                        text = dest_f.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        pass
                    else:
                        dest_f.write_text(_tokenise_home(text, home), encoding="utf-8")
                    files_copied += 1
                    rels.append(str(rel))
            for key, tree in snap.extra_trees.items():
                files_copied += _copy_tree(tree, staging / key)
            dconf_blob = ""
            if snap.desktop in {"gnome", "budgie", "cinnamon"} and shutil.which("dconf"):
                dump_paths = {
                    "gnome": "/org/gnome/desktop/",
                    "budgie": "/org/gnome/desktop/",
                    "cinnamon": "/org/cinnamon/",
                }
                dconf_blob = _run(["dconf", "dump", dump_paths[snap.desktop]], timeout=15)
                if dconf_blob:
                    dpath = cfg / "dconf-desktop.ini"
                    dpath.parent.mkdir(parents=True, exist_ok=True)
                    dpath.write_text(_tokenise_home(dconf_blob, home), encoding="utf-8")
                    files_copied += 1
                    rels.append("dconf-desktop.ini")
            note("layout", files=rels)

        pack_name = name.strip() or f"{desktop_label(snap.desktop)} look"
        manifest = {
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "name": pack_name,
            "desktop": snap.desktop,
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "summary": snap.summary(),
            "items": bundled,
            "files_copied": files_copied,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        _progress("Writing archive…", 90)
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_archive(staging, dest)
        _progress("Look pack saved", 100)
        print(f"DEST={dest}", flush=True)
        return dest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_archive(src: Path, dest: Path) -> None:
    suffixes = "".join(dest.suffixes).lower()
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in src.rglob("*"):
                if path.is_file():
                    zf.write(path, path.relative_to(src).as_posix())
        return
    mode = "w"
    if suffixes.endswith(".tar.gz") or suffixes.endswith(".tgz"):
        mode = "w:gz"
    elif suffixes.endswith(".tar.xz") or suffixes.endswith(".txz"):
        mode = "w:xz"
    elif suffixes.endswith(".tar.bz2") or suffixes.endswith(".tbz2"):
        mode = "w:bz2"
    elif suffixes.endswith(".tar"):
        mode = "w"
    else:
        mode = "w:xz"
        if dest.suffix != ".xz":
            dest = dest.with_name(dest.name + ".tar.xz")
    with tarfile.open(dest, mode) as tf:
        tf.add(src, arcname=".")


# ---------------------------------------------------------------------------
# Inspect / install archives
# ---------------------------------------------------------------------------
@dataclass
class ArchiveInfo:
    path: Path
    kind: str  # urstack-look | icons | gtk | plasma-lookandfeel | plasmoid | wallpaper | mixed | unknown
    name: str
    desktop: str
    summary: str
    items: list[str]
    file_count: int
    unsafe: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "kind": self.kind,
            "name": self.name,
            "desktop": self.desktop,
            "summary": self.summary,
            "items": self.items,
            "file_count": self.file_count,
            "unsafe": self.unsafe,
        }


def _archive_names(path: Path) -> list[str]:
    path = Path(path)
    suffixes = "".join(path.suffixes).lower()
    names: list[str] = []
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                names.append(info.filename.replace("\\", "/"))
        return names
    with tarfile.open(path, "r:*") as tf:
        for m in tf.getmembers():
            names.append(m.name.replace("\\", "/"))
    return names


def _validate_names(names: Iterable[str]) -> str:
    for name in names:
        if not name or name.endswith("/"):
            continue
        if name.startswith("/") or name.startswith("\\") or name.startswith("~"):
            return f"absolute path: {name}"
        parts = Path(name.replace("\\", "/")).parts
        if ".." in parts:
            return f"path traversal: {name}"
    return ""


def inspect_archive(path: Path) -> ArchiveInfo:
    path = Path(path)
    if not path.is_file():
        raise LookError(f"No such file: {path}")
    try:
        names = _archive_names(path)
    except (tarfile.TarError, zipfile.BadZipFile, OSError) as exc:
        raise LookError(f"Not a readable theme archive: {exc}") from exc
    unsafe = _validate_names(names)
    posix = [n.replace("\\", "/").lstrip("./") for n in names]
    kind = "unknown"
    name = path.stem
    desktop = ""
    items: list[str] = []
    summary = ""

    manifest: dict[str, Any] | None = None
    if any(n.rstrip("/").endswith("manifest.json") or n == "manifest.json" or n.endswith("/manifest.json") for n in posix):
        raw = _read_archive_file(path, "manifest.json")
        if raw:
            try:
                manifest = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                manifest = None
    if isinstance(manifest, dict) and manifest.get("format") == FORMAT:
        kind = FORMAT
        name = str(manifest.get("name") or name)
        desktop = str(manifest.get("desktop") or "")
        summary = str(manifest.get("summary") or "")
        bundled = manifest.get("items") or {}
        if isinstance(bundled, dict):
            items = [k for k, v in bundled.items() if v]
        return ArchiveInfo(
            path=path,
            kind=kind,
            name=name,
            desktop=desktop,
            summary=summary,
            items=items,
            file_count=len(posix),
            unsafe=unsafe,
        )

    joined = "\n".join(posix)
    if re.search(r"(^|/)index\.theme$", joined, re.M) and re.search(
        r"(scalable|16x16|22x22|24x24|32x32|48x48|cursors)/", joined
    ):
        kind = "icons"
        items.append("icons")
        name = _top_theme_name(posix, "index.theme") or name
    if re.search(r"(^|/)gtk-3\.0/|(^|/)gtk-4\.0/|(^|/)gnome-shell/", joined):
        if kind == "unknown":
            kind = "gtk"
        elif kind != "gtk":
            kind = "mixed"
        items.append("gtk")
        name = _top_theme_name(posix, "index.theme") or name
    if "Plasma/LookAndFeel" in joined or re.search(r"look-and-feel/", joined):
        kind = "plasma-lookandfeel" if kind == "unknown" else "mixed"
        items.append("lookandfeel")
    if re.search(r"(^|/)contents/ui/.+\.qml$", joined) or "Plasma/Applet" in joined:
        kind = "plasmoid" if kind == "unknown" else "mixed"
        items.append("widgets")
    if re.search(r"\.colors$", joined):
        items.append("colors")
        kind = "mixed" if kind not in {"unknown", "mixed"} else kind
    images = [n for n in posix if Path(n).suffix.lower() in _IMAGE_SUFFIXES]
    if kind == "unknown" and images and len(images) == len(
        [n for n in posix if not n.endswith("/")]
    ):
        kind = "wallpaper"
        items.append("wallpaper")
    if not items and images:
        items.append("wallpaper")
        kind = "wallpaper" if kind == "unknown" else kind
    if not summary:
        summary = ", ".join(items) if items else "Theme archive"
    return ArchiveInfo(
        path=path,
        kind=kind,
        name=name,
        desktop=desktop,
        summary=summary,
        items=list(dict.fromkeys(items)),
        file_count=len([n for n in posix if not n.endswith("/")]),
        unsafe=unsafe,
    )


def _top_theme_name(names: list[str], marker: str) -> str:
    for n in names:
        p = Path(n.replace("\\", "/"))
        if p.name == marker and p.parent != Path("."):
            return p.parent.name
    return ""


def _read_archive_file(path: Path, want: str) -> bytes | None:
    suffixes = "".join(path.suffixes).lower()
    want = want.lstrip("./")
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.replace("\\", "/").lstrip("./") == want or name.endswith("/" + want):
                    return zf.read(name)
        return None
    with tarfile.open(path, "r:*") as tf:
        for m in tf.getmembers():
            n = m.name.replace("\\", "/").lstrip("./")
            if n == want or n.endswith("/" + want):
                extracted = tf.extractfile(m)
                return extracted.read() if extracted else None
    return None


def _extract_all(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    names = _archive_names(path)
    bad = _validate_names(names)
    if bad:
        raise LookError(f"Refusing archive ({bad})")
    suffixes = "".join(path.suffixes).lower()
    if suffixes.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                rel = _safe_rel(info.filename.replace("\\", "/"))
                if rel is None:
                    if info.filename.endswith("/"):
                        continue
                    raise LookError(f"Refusing unsafe path: {info.filename}")
                target = dest / rel
                if info.is_dir() or info.filename.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return
    with tarfile.open(path, "r:*") as tf:
        for member in tf.getmembers():
            rel = _safe_rel(member.name.replace("\\", "/"))
            if rel is None:
                if member.isdir():
                    continue
                raise LookError(f"Refusing unsafe path: {member.name}")
            if member.issym() or member.islnk():
                continue
            target = dest / rel
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted, open(target, "wb") as out:
                shutil.copyfileobj(extracted, out)


def _unwrap_single_dir(root: Path) -> Path:
    entries = [p for p in root.iterdir() if p.name not in {".", ".."}]
    if len(entries) == 1 and entries[0].is_dir() and not (entries[0] / "manifest.json").is_file():
        return entries[0]
    return root


def install_archive(
    path: Path,
    *,
    home: Path | None = None,
    apply: bool = True,
    environ: dict[str, str] | None = None,
) -> dict[str, Any]:
    home = Path(home) if home is not None else Path.home()
    path = Path(path)
    _progress("Inspecting archive…", 8)
    info = inspect_archive(path)
    if info.unsafe:
        raise LookError(f"Refusing archive ({info.unsafe})")
    staging = Path(tempfile.mkdtemp(prefix="urstack-look-in-"))
    installed: list[str] = []
    try:
        _progress("Extracting…", 20)
        _extract_all(path, staging)
        root = staging
        if (staging / "manifest.json").is_file():
            pass
        else:
            # Some packs nest everything under one folder.
            for candidate in staging.iterdir():
                if candidate.is_dir() and (candidate / "manifest.json").is_file():
                    root = candidate
                    break
            else:
                root = _unwrap_single_dir(staging)

        manifest: dict[str, Any] = {}
        man_path = root / "manifest.json"
        if man_path.is_file():
            try:
                manifest = json.loads(man_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {}

        _progress("Installing into your home directory…", 45)
        if manifest.get("format") == FORMAT:
            installed.extend(_install_urstack_pack(root, home, manifest))
        else:
            installed.extend(_install_third_party(root, home, info))

        applied: list[str] = []
        if apply:
            _progress("Applying the look…", 80)
            applied = apply_look(home, manifest if manifest.get("format") == FORMAT else None, environ=environ)

        _progress("Look installed", 100)
        result = {
            "ok": True,
            "name": info.name,
            "kind": info.kind,
            "installed": installed,
            "applied": applied,
            "need_logout": bool(
                {"widgets", "layout", "lookandfeel"} & set(info.items)
                or (manifest.get("items") or {}).get("widgets")
                or (manifest.get("items") or {}).get("layout")
            ),
        }
        print("REPORT=" + json.dumps(result), flush=True)
        return result
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _install_urstack_pack(root: Path, home: Path, manifest: dict[str, Any]) -> list[str]:
    installed: list[str] = []
    mapping = [
        ("wallpaper", home / ".local/share/wallpapers/urstack"),
        ("icons", home / ".local/share/icons"),
        ("cursors", home / ".local/share/icons"),
        ("gtk-themes", home / ".local/share/themes"),
        ("fonts", home / ".local/share/fonts/urstack"),
        ("kvantum", home / ".config/Kvantum"),
        ("cosmic", home / ".config/cosmic"),
        ("aurorae", home / ".local/share/aurorae"),
    ]
    for rel, dest in mapping:
        src = root / rel
        if src.is_dir():
            # wallpaper/fonts are a bag of files; others are theme dirs
            if rel in {"wallpaper", "fonts"}:
                dest.mkdir(parents=True, exist_ok=True)
                for f in src.iterdir():
                    if f.is_file() and _copy_file(f, dest / f.name):
                        installed.append(f"{rel}/{f.name}")
            else:
                for child in src.iterdir():
                    if child.is_dir():
                        _copy_tree(child, dest / child.name)
                        installed.append(f"{rel}/{child.name}")
                    elif child.is_file():
                        if _copy_file(child, dest / child.name):
                            installed.append(f"{rel}/{child.name}")
    plasma_src = root / "plasma"
    plasma_dest = {
        "look-and-feel": home / ".local/share/plasma/look-and-feel",
        "plasmoids": home / ".local/share/plasma/plasmoids",
        "color-schemes": home / ".local/share/color-schemes",
        "desktoptheme": home / ".local/share/plasma/desktoptheme",
    }
    if plasma_src.is_dir():
        for sub, dest in plasma_dest.items():
            src = plasma_src / sub
            if not src.is_dir():
                continue
            for child in src.iterdir():
                if child.is_dir():
                    _copy_tree(child, dest / child.name)
                    installed.append(f"plasma/{sub}/{child.name}")
                elif child.is_file():
                    if _copy_file(child, dest / child.name):
                        installed.append(f"plasma/{sub}/{child.name}")
    cfg = root / "config"
    if cfg.is_dir():
        for src in cfg.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(cfg)
            dest = home / rel
            if rel.as_posix() == "dconf-desktop.ini":
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            text = src.read_bytes()
            try:
                decoded = text.decode("utf-8")
            except UnicodeDecodeError:
                dest.write_bytes(text)
            else:
                dest.write_text(_untokenise_home(decoded, home), encoding="utf-8")
            installed.append(f"config/{rel.as_posix()}")
        dconf_ini = cfg / "dconf-desktop.ini"
        if dconf_ini.is_file() and shutil.which("dconf"):
            blob = _untokenise_home(dconf_ini.read_text(encoding="utf-8"), home)
            desktop = str(manifest.get("desktop") or "")
            load_path = {
                "gnome": "/org/gnome/desktop/",
                "budgie": "/org/gnome/desktop/",
                "cinnamon": "/org/cinnamon/",
            }.get(desktop, "/org/gnome/desktop/")
            subprocess.run(
                ["dconf", "load", load_path],
                input=blob,
                text=True,
                timeout=20,
                check=False,
                capture_output=True,
            )
            installed.append("dconf-desktop.ini")
    return installed


def _dir_looks_like_icons(path: Path) -> bool:
    return (path / "index.theme").is_file() and (
        (path / "cursors").is_dir()
        or any((path / d).is_dir() for d in ("scalable", "16x16", "22x22", "24x24", "48x48"))
    )


def _dir_looks_like_gtk(path: Path) -> bool:
    return (path / "gtk-3.0").is_dir() or (path / "gtk-4.0").is_dir() or (
        path / "gnome-shell"
    ).is_dir()


def _install_third_party(root: Path, home: Path, info: ArchiveInfo) -> list[str]:
    installed: list[str] = []
    candidates = [root, *([p for p in root.iterdir() if p.is_dir()])]
    for path in candidates:
        if _dir_looks_like_icons(path):
            dest = home / ".local/share/icons" / path.name
            _copy_tree(path, dest)
            installed.append(f"icons/{path.name}")
        elif _dir_looks_like_gtk(path):
            dest = home / ".local/share/themes" / path.name
            _copy_tree(path, dest)
            installed.append(f"gtk/{path.name}")
        elif (path / "metadata.json").is_file():
            blob = (path / "metadata.json").read_text(encoding="utf-8", errors="replace")
            if "LookAndFeel" in blob:
                dest = home / ".local/share/plasma/look-and-feel" / path.name
                _copy_tree(path, dest)
                installed.append(f"look-and-feel/{path.name}")
            elif "Plasma/Applet" in blob or "plasmoid" in blob.lower():
                dest = home / ".local/share/plasma/plasmoids" / path.name
                _copy_tree(path, dest)
                installed.append(f"plasmoids/{path.name}")
            elif "Wallpaper" in blob:
                dest = home / ".local/share/wallpapers" / path.name
                _copy_tree(path, dest)
                installed.append(f"wallpapers/{path.name}")
        elif path.suffix == ".colors" and path.is_file():
            dest = home / ".local/share/color-schemes" / path.name
            if _copy_file(path, dest):
                installed.append(f"colors/{path.name}")
        elif path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
            dest = home / ".local/share/wallpapers/urstack" / path.name
            if _copy_file(path, dest):
                installed.append(f"wallpaper/{path.name}")
    if not installed:
        # Flatten: copy images as wallpaper.
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
                dest = home / ".local/share/wallpapers/urstack" / path.name
                if _copy_file(path, dest):
                    installed.append(f"wallpaper/{path.name}")
        if not installed:
            raise LookError(
                "Could not tell what this archive is. "
                "UrStack installs look packs, icon themes, GTK themes, "
                "Plasma look-and-feel / widgets, and wallpaper images."
            )
    return installed


def apply_look(
    home: Path,
    manifest: dict[str, Any] | None,
    *,
    environ: dict[str, str] | None = None,
) -> list[str]:
    applied: list[str] = []
    desktop = detect_desktop(environ)
    items = (manifest or {}).get("items") or {}
    icons = (items.get("icons") or {}).get("name") if isinstance(items.get("icons"), dict) else ""
    cursors = (items.get("cursors") or {}).get("name") if isinstance(items.get("cursors"), dict) else ""
    gtk = (items.get("gtk") or {}).get("name") if isinstance(items.get("gtk"), dict) else ""
    colors = (items.get("colors") or {}).get("name") if isinstance(items.get("colors"), dict) else ""
    lookandfeel = (
        (items.get("lookandfeel") or {}).get("name")
        if isinstance(items.get("lookandfeel"), dict)
        else ""
    )
    walls = (items.get("wallpaper") or {}).get("files") if isinstance(items.get("wallpaper"), dict) else []
    wall_path = ""
    if walls:
        candidate = home / ".local/share/wallpapers/urstack" / str(walls[0])
        if candidate.is_file():
            wall_path = str(candidate)

    if icons:
        if desktop == "plasma":
            _kwrite("kdeglobals", "Icons", "Theme", icons, home=home)
        for schema in (
            "org.gnome.desktop.interface",
            "org.cinnamon.desktop.interface",
            "org.mate.interface",
        ):
            if _gsettings_set(schema, "icon-theme", icons):
                applied.append(f"icons={icons}")
                break
        else:
            if icons:
                applied.append(f"icons={icons}")
    if cursors:
        _kwrite("kcminputrc", "Mouse", "cursorTheme", cursors, home=home)
        _gsettings_set("org.gnome.desktop.interface", "cursor-theme", cursors)
        applied.append(f"cursors={cursors}")
    if gtk:
        _gsettings_set("org.gnome.desktop.interface", "gtk-theme", gtk)
        _gsettings_set("org.cinnamon.desktop.interface", "gtk-theme", gtk)
        applied.append(f"gtk={gtk}")
    if colors and shutil.which("plasma-apply-colorscheme"):
        if _run(["plasma-apply-colorscheme", colors]):
            applied.append(f"colors={colors}")
    elif colors:
        _kwrite("kdeglobals", "General", "ColorScheme", colors, home=home)
        applied.append(f"colors={colors}")
    if lookandfeel and shutil.which("plasma-apply-lookandfeel"):
        _run(["plasma-apply-lookandfeel", "-a", lookandfeel], timeout=20)
        applied.append(f"lookandfeel={lookandfeel}")
    if wall_path:
        if shutil.which("plasma-apply-wallpaperimage"):
            _run(["plasma-apply-wallpaperimage", wall_path], timeout=15)
            applied.append("wallpaper")
        uri = f"file://{wall_path}"
        _gsettings_set("org.gnome.desktop.background", "picture-uri", uri)
        _gsettings_set("org.gnome.desktop.background", "picture-uri-dark", uri)
        _gsettings_set("org.cinnamon.desktop.background", "picture-uri", uri)
    return applied


def _kwrite(file_name: str, group: str, key: str, value: str, *, home: Path) -> None:
    if shutil.which("kwriteconfig6"):
        subprocess.run(
            [
                "kwriteconfig6",
                "--file",
                file_name,
                "--group",
                group,
                "--key",
                key,
                value,
            ],
            check=False,
            capture_output=True,
            timeout=8,
        )
        return
    path = home / ".config" / file_name
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _parse_kv_file(path)
    data.setdefault(group, {})[key] = value
    lines: list[str] = []
    for sec, keys in data.items():
        if sec:
            lines.append(f"[{sec}]")
        for k, v in keys.items():
            lines.append(f"{k}={v}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="urstack-look")
    p.add_argument("--home", default="", help="Override $HOME (tests)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Print the live look as JSON")

    exp = sub.add_parser("export", help="Pack the current look into a tar")
    exp.add_argument("--out", required=True, help="Destination .tar.xz / .zip")
    exp.add_argument("--include", default="", help="Comma list of " + ",".join(INCLUDE_KEYS))
    exp.add_argument("--name", default="")

    ins = sub.add_parser("inspect", help="Describe a theme archive")
    ins.add_argument("archive")

    inst = sub.add_parser("install", help="Install a theme archive for this user")
    inst.add_argument("archive")
    inst.add_argument("--no-apply", action="store_true")

    args = p.parse_args(argv)
    home = Path(args.home).expanduser() if args.home else Path.home()
    try:
        if args.cmd == "status":
            _print_json(inspect_look(home).as_dict())
            return 0
        if args.cmd == "export":
            export_look(Path(args.out).expanduser(), home=home, include=args.include, name=args.name)
            return 0
        if args.cmd == "inspect":
            _print_json(inspect_archive(Path(args.archive).expanduser()).as_dict())
            return 0
        if args.cmd == "install":
            install_archive(
                Path(args.archive).expanduser(),
                home=home,
                apply=not args.no_apply,
            )
            return 0
    except LookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"# {exc}", flush=True)
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
