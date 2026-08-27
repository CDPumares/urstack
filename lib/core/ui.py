#!/usr/bin/env python3
"""Polished GTK4 / libadwaita dialogs for UrStack.

Modes (first argument):
  hub         Results + actions → apply|backup|restore|apps|settings|log|runs|close
  checklist   Multi-select list → id1|id2|...
  radio       Single-select list → id
  text        Scrollable text / file viewer
  message     Info / error message
  ask         Yes/No question
  folder      Directory picker
  progress    Progress UI (zenity-compatible stdin)
  runs        Browse per-run log folders
  settings    Toggle sources / rescan → saved|rescan|close
  catalog     Popular apps by category → install|… / install-batch|… or close

Item files (checklist/radio): one line per item
  CHECKED|id|Label
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, GObject, Gtk, Pango  # noqa: E402
from datetime import UTC, datetime

_CORE_DIR = str(Path(__file__).resolve().parent)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)
from page_icons import PAGE_ICON_CANDIDATES, svg_reads_as_symbolic  # noqa: E402
import look as look_engine  # noqa: E402
import theme_store as theme_store_mod  # noqa: E402
import user_catalog  # noqa: E402

DEFAULT_W, DEFAULT_H = 1440, 920
# Shell pages fill the content pane; clamp only kicks in on very wide displays.
CONTENT_MAX = 2400
# Outer left/right inset for every shell page (clamp + Apps).
PAGE_SIDE_PAD = 24
# Must match installed desktop file basename (com.local.urstack.desktop)
URSTACK_APP_ID = "com.local.urstack"
APP_ROOT = Path(__file__).resolve().parents[2]
APP_ICON = APP_ROOT / "data" / "icons" / "urstack.png"
if not APP_ICON.is_file():
    APP_ICON = APP_ROOT / "data" / "icons" / "stackup.png"
if not APP_ICON.is_file():
    APP_ICON = APP_ROOT / "data" / "icons" / "fedora-updates.png"


def _theme_icon_svg(theme: Gtk.IconTheme, name: str) -> str:
    """Read the SVG bytes for a theme icon, or '' if it is not an SVG file."""
    try:
        flags = Gtk.IconLookupFlags(0)
        icon = theme.lookup_icon(name, None, 16, 1, Gtk.TextDirection.NONE, flags)
        if icon is None:
            return ""
        gfile = icon.get_file()
        if gfile is None:
            return ""
        path = gfile.get_path()
        if not path or not str(path).endswith(".svg"):
            return ""
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, TypeError, ValueError, GLib.Error):
        return ""


_ICON_THEMES_SEEDED: set[int] = set()


def _ensure_bundled_icon_path(theme: Gtk.IconTheme) -> None:
    """So source and install trees find urstack-look-symbolic under data/icons."""
    key = id(theme)
    if key in _ICON_THEMES_SEEDED:
        return
    extra = APP_ROOT / "data" / "icons"
    if extra.is_dir():
        try:
            theme.add_search_path(str(extra))
        except Exception:  # noqa: BLE001
            return
    _ICON_THEMES_SEEDED.add(key)


def pick_icon(*names: str) -> str:
    """Return the first icon the current theme actually provides."""
    display = Gdk.Display.get_default()
    theme = (
        Gtk.IconTheme.get_for_display(display)
        if display is not None
        else Gtk.IconTheme.new()
    )
    _ensure_bundled_icon_path(theme)
    for name in names:
        if not name or not theme.has_icon(name):
            continue
        svg = _theme_icon_svg(theme, name)
        if svg and not svg_reads_as_symbolic(svg):
            continue
        return name
    return names[0] if names else "image-missing-symbolic"


def page_icon(key: str) -> str:
    return pick_icon(*PAGE_ICON_CANDIDATES.get(key, ()))


# Accent colours come from the UrStack mark (navy / electric blue / magenta).
STYLE_SHEET = APP_ROOT / "data" / "ui" / "style.css"


@dataclass
class Item:
    checked: bool
    item_id: str
    label: str


@dataclass
class Section:
    title: str
    body: str
    kind: str = "update"  # update | advisory | empty


def parse_items_file(path: str) -> list[Item]:
    items: list[Item] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("|", 2)
            if len(parts) < 3:
                continue
            checked = parts[0].strip().upper() in {"TRUE", "1", "YES", "ON"}
            items.append(Item(checked=checked, item_id=parts[1].strip(), label=parts[2].strip()))
    return items


def read_text(path: str | None, inline: str | None) -> str:
    if path:
        try:
            return open(path, encoding="utf-8", errors="replace").read()
        except OSError as exc:
            return f"(Could not read file: {exc})"
    return inline or ""


def parse_sections(text: str) -> list[Section]:
    """Split check results into titled sections for card layout."""
    # Older builders embedded literal \n; normalize before splitting.
    text = (text or "").replace("\\n", "\n").strip()
    if not text:
        return [Section(title="Status", body="Nothing to show.", kind="empty")]

    parts = re.split(r"(?m)^(=== .+? ===)\s*$", text)
    sections: list[Section] = []
    if parts and parts[0].strip() and not parts[0].strip().startswith("==="):
        preamble = parts[0].strip()
        kind = "empty" if preamble.lower().startswith("nothing to update") else "update"
        sections.append(Section(title="Overview", body=preamble, kind=kind))

    i = 1
    while i + 1 < len(parts):
        title = parts[i].strip().strip("=").strip()
        body = parts[i + 1].strip()
        kind = (
            "advisory"
            if re.search(r"advisory|oci|preflight|appimage|tip:|changelog", title + body, re.I)
            else "update"
        )
        if body.lower().startswith("nothing to update"):
            kind = "empty"
        sections.append(Section(title=title, body=body or "(no details)", kind=kind))
        i += 2

    if not sections:
        sections.append(Section(title="Results", body=text, kind="update"))
    return sections


def pretty_package_line(line: str) -> tuple[str, str]:
    """Return (title, subtitle) for a package/update line."""
    raw = line.strip()
    if not raw:
        return "", ""
    # Flatpak app ids
    if re.match(r"^[A-Za-z0-9._-]+\.[A-Za-z0-9._-]+", raw) and " " not in raw.split("/")[0]:
        app_id = raw.split("/")[0]
        short = app_id.split(".")[-1].replace("-", " ").replace("_", " ")
        short = " ".join(p[:1].upper() + p[1:] for p in short.split())
        return short, app_id
    # key=value pairs (node current/latest)
    if "=" in raw and " " not in raw.split("=", 1)[0]:
        k, v = raw.split("=", 1)
        return k.replace("_", " ").title(), v
    # Current: / Latest:
    if ":" in raw and raw.index(":") < 20:
        k, v = raw.split(":", 1)
        return k.strip(), v.strip()
    cleaned = re.sub(r"^\(oci noise\)\s*", "", raw, flags=re.I)
    if cleaned != raw:
        return cleaned, "Runtime / OCI note"
    return raw, ""


def load_css() -> None:
    try:
        css = STYLE_SHEET.read_bytes()
    except OSError as exc:
        # An unstyled window is still usable, so this must not be fatal.
        print(f"urstack: cannot load stylesheet {STYLE_SHEET}: {exc}", file=sys.stderr)
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    display = Gdk.Display.get_default()
    if display:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


APPEARANCE_VALUES = ("system", "light", "dark")
_THEME_SYNC_CONNECTED = {"v": False}


def default_config_path() -> Path:
    env = (os.environ.get("FEDORA_UPDATES_USER_CONFIG") or "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "urstack" / "config.conf"


def normalize_appearance(value: str | None) -> str:
    v = (value or "system").strip().lower()
    return v if v in APPEARANCE_VALUES else "system"


def read_appearance(config_file: str | Path | None = None) -> str:
    path = Path(config_file).expanduser() if config_file else default_config_path()
    return normalize_appearance(read_config_map(path).get("appearance"))


def _sync_window_theme_classes(*_a: object) -> None:
    """Tag windows so CSS can diverge for light vs dark."""
    try:
        dark = bool(Adw.StyleManager.get_default().get_dark())
    except Exception:  # noqa: BLE001
        return
    try:
        tops = Gtk.Window.get_toplevels()
    except Exception:  # noqa: BLE001
        return
    try:
        count = tops.get_n_items()
    except Exception:  # noqa: BLE001
        return
    for i in range(count):
        win = tops.get_item(i)
        if win is None:
            continue
        try:
            win.remove_css_class("fu-light")
            win.remove_css_class("fu-dark")
            win.add_css_class("fu-dark" if dark else "fu-light")
        except Exception:  # noqa: BLE001
            pass


def apply_appearance(value: str) -> None:
    schemes = {
        "light": Adw.ColorScheme.FORCE_LIGHT,
        "dark": Adw.ColorScheme.FORCE_DARK,
        "system": Adw.ColorScheme.DEFAULT,
    }
    try:
        Adw.StyleManager.get_default().set_color_scheme(
            schemes.get(normalize_appearance(value), Adw.ColorScheme.DEFAULT)
        )
    except Exception:  # noqa: BLE001
        pass
    _sync_window_theme_classes()


def _ensure_theme_sync() -> None:
    if _THEME_SYNC_CONNECTED["v"]:
        return
    _THEME_SYNC_CONNECTED["v"] = True
    try:
        Adw.StyleManager.get_default().connect(
            "notify::dark", lambda *_a: _sync_window_theme_classes()
        )
    except Exception:  # noqa: BLE001
        pass


def load_app_pixbuf(pixel_size: int) -> GdkPixbuf.Pixbuf | None:
    if not APP_ICON.is_file():
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(str(APP_ICON), pixel_size, pixel_size, True)
    except GLib.Error:
        return None


def app_icon_image(pixel_size: int = 56) -> Gtk.Widget:
    # Always scale via pixbuf so size is exact and PNG alpha is preserved
    # (Texture.new_from_filename keeps the full bitmap and looked like a black tile).
    pix = load_app_pixbuf(pixel_size)
    if pix is not None:
        try:
            tex = Gdk.Texture.new_for_pixbuf(pix)
            img = Gtk.Image.new_from_paintable(tex)
        except Exception:  # noqa: BLE001
            img = Gtk.Image.new_from_pixbuf(pix)
        img.set_pixel_size(pixel_size)
        img.set_valign(Gtk.Align.CENTER)
        img.add_css_class("fu-app-icon")
        return img
    img = Gtk.Image.new_from_icon_name("system-software-update-symbolic")
    img.set_pixel_size(pixel_size)
    return img


def preferred_window_size(fallback_w: int = DEFAULT_W, fallback_h: int = DEFAULT_H) -> tuple[int, int]:
    """Size main windows from the active monitor so they feel roomy on large displays."""
    try:
        display = Gdk.Display.get_default()
        if display is None:
            return fallback_w, fallback_h
        monitors = display.get_monitors()
        mon = monitors.get_item(0) if monitors is not None else None
        if mon is None:
            return fallback_w, fallback_h
        geo = mon.get_geometry()
        w = max(1100, min(int(geo.width * 0.75), 1500))
        h = max(760, min(int(geo.height * 0.80), 1050))
        return w, h
    except Exception:  # noqa: BLE001
        return fallback_w, fallback_h


def make_window(
    app: Adw.Application,
    title: str,
    w: int | None = None,
    h: int | None = None,
    *,
    compact: bool = False,
) -> Adw.ApplicationWindow:
    if w is None or h is None:
        pw, ph = preferred_window_size()
        w = pw if w is None else w
        h = ph if h is None else h
    win = Adw.ApplicationWindow(application=app, title=title)
    win.set_default_size(w, h)
    if compact:
        win.set_size_request(min(420, w), min(160, h))
        win.set_resizable(False)
    else:
        win.set_size_request(min(960, w), min(640, h))
    win.add_css_class("urstack")
    try:
        dark = bool(Adw.StyleManager.get_default().get_dark())
        win.add_css_class("fu-dark" if dark else "fu-light")
    except Exception:  # noqa: BLE001
        pass
    try:
        win.set_icon_name("urstack")
    except Exception:  # noqa: BLE001
        pass
    return win


def header_title_widget(title: str, subtitle: str | None = None) -> Gtk.Widget:
    """Centered title for HeaderBar. Empty title → blank (home screen)."""
    if not (title or "").strip() and not (subtitle or "").strip():
        return Gtk.Box()
    try:
        wt = Adw.WindowTitle(title=(title or "").strip() or "UrStack")
        if subtitle and subtitle.strip():
            wt.set_subtitle(subtitle.strip())
        return wt
    except Exception:  # noqa: BLE001
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_box.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=title, xalign=0.5)
        t.add_css_class("heading")
        title_box.append(t)
        if subtitle:
            s = Gtk.Label(label=subtitle, xalign=0.5)
            s.add_css_class("dim-label")
            s.add_css_class("caption")
            title_box.append(s)
        return title_box


def wrap_shell(
    win: Adw.ApplicationWindow,
    title: str,
    content: Gtk.Widget,
    subtitle: str | None = None,
) -> Adw.ToolbarView:
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_title_widget(header_title_widget(title, subtitle))
    try:
        header.set_show_title(bool((title or "").strip() or (subtitle or "").strip()))
    except Exception:  # noqa: BLE001
        pass
    # Don't pack a brand icon here — the window already shows one in the
    # client-side decoration (packing another creates a duplicate).
    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    win.set_content(toolbar)
    return toolbar


def make_nav_page(
    title: str,
    content: Gtk.Widget,
    subtitle: str | None = None,
    *,
    tag: str | None = None,
) -> Adw.NavigationPage:
    """One screen inside Adw.NavigationView.

    Back navigation is always the standard HeaderBar back control that
    NavigationView provides — no per-page bottom Back buttons.
    """
    page_title = (title or "").strip() or "Home"
    page = Adw.NavigationPage(title=page_title)
    try:
        page.set_tag(tag or page_title.lower().replace(" ", "-"))
    except Exception:  # noqa: BLE001
        pass

    # Always keep a HeaderBar so CSD window controls (min/max/close) remain.
    # Title text stays blank like Overview — page heroes already name the section.
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_title_widget(header_title_widget("", None))
    try:
        header.set_show_title(False)
    except Exception:  # noqa: BLE001
        pass
    try:
        # Keep min/max/close; omit "icon" so desktops don't inject the tiny app mark.
        header.set_decoration_layout(":minimize,maximize,close")
        header.set_show_end_title_buttons(True)
        header.set_show_start_title_buttons(False)
    except Exception:  # noqa: BLE001
        pass
    try:
        # Let NavigationView own the back chevron on pushed pages
        header.set_show_back_button(bool((title or "").strip()))
    except Exception:  # noqa: BLE001
        pass

    toolbar.add_top_bar(header)
    toolbar.set_content(content)
    page.set_child(toolbar)
    return page


def spinning_icon(icon_name: str = "view-refresh-symbolic", pixel_size: int = 36) -> Gtk.Image:
    """Rotating symbolic spinner used for all scan/loading states."""
    icon = Gtk.Image.new_from_icon_name(icon_name or "view-refresh-symbolic")
    icon.set_pixel_size(pixel_size)
    icon.add_css_class("fu-icon-spin")
    return icon


def page_toolbar(
    title: str,
    subtitle: str = "",
    *,
    icon_name: str | None = None,
    spin_icon: bool = False,
) -> Gtk.Widget:
    """Shared Apps-like header band for tool pages (Apply, Backup, Settings…)."""
    band = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    band.add_css_class("fu-page-toolbar")
    band.set_hexpand(True)

    if icon_name:
        if spin_icon:
            icon = spinning_icon(icon_name, 36)
        else:
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(36)
        icon.set_valign(Gtk.Align.START)
        band.append(icon)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    texts.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0.0, wrap=True)
    t.add_css_class("fu-page-title")
    texts.append(t)
    if subtitle:
        s = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
        s.add_css_class("fu-page-sub")
        s.add_css_class("dim-label")
        texts.append(s)
    band.append(texts)
    return band


def page_hero(
    score: str,
    score_label: str,
    title: str,
    subtitle: str,
    *,
    warn: bool = False,
    ok: bool = False,
    trailing: Gtk.Widget | None = None,
    heading: str = "",
    heading_sub: str = "",
    icon_name: str | None = None,
    heading_trailing: Gtk.Widget | None = None,
) -> Gtk.Widget:
    """Page hero: section header + icon on the left, status content on the right."""
    hero_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    hero_box.add_css_class("fu-page-hero")
    if warn:
        hero_box.add_css_class("fu-page-hero-warn")
    elif ok:
        hero_box.add_css_class("fu-page-hero-ok")

    head = None
    if heading or icon_name:
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        head.add_css_class("fu-page-hero-head")
        head.set_valign(Gtk.Align.CENTER)
        if icon_name:
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(40)
            icon.set_valign(Gtk.Align.START)
            head.append(icon)
            if heading or heading_sub:
                texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                texts.set_hexpand(True)
                if heading:
                    hl = Gtk.Label(label=heading, xalign=0.0, wrap=True)
                    hl.add_css_class("fu-hero-title")
                    texts.append(hl)
                if heading_sub:
                    hs = Gtk.Label(label=heading_sub, xalign=0.0, wrap=True)
                    hs.add_css_class("fu-hero-sub")
                    texts.append(hs)
                head.append(texts)
        if heading_trailing is not None:
            heading_trailing.set_valign(Gtk.Align.CENTER)
            head.append(heading_trailing)
        head.set_hexpand(True)
        head.set_halign(Gtk.Align.FILL)

    body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    body.add_css_class("fu-page-hero-body")
    body.set_hexpand(True)
    body.set_halign(Gtk.Align.FILL)
    body.set_valign(Gtk.Align.CENTER)

    if (score or "").strip():
        score_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sc = Gtk.Label(label=score, xalign=0.0)
        sc.add_css_class("fu-page-score")
        score_col.append(sc)
        sl = Gtk.Label(label=score_label, xalign=0.0)
        sl.add_css_class("fu-page-score-label")
        score_col.append(sl)
        body.append(score_col)

    if title or subtitle:
        text_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_col.set_hexpand(True)
        if title:
            ht = Gtk.Label(label=title, xalign=0.0, wrap=True)
            ht.add_css_class("fu-hero-title")
            text_col.append(ht)
        if subtitle:
            hs = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
            hs.add_css_class("fu-hero-sub")
            text_col.append(hs)
        body.append(text_col)

    if trailing is not None:
        trailing.set_valign(Gtk.Align.CENTER)
        body.append(trailing)

    if head is not None:
        # Equal panes so the vertical rule sits on the hero midline on every page.
        panes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        panes.add_css_class("fu-page-hero-panes")
        panes.set_homogeneous(True)
        panes.set_hexpand(True)
        panes.set_halign(Gtk.Align.FILL)
        panes.append(head)
        panes.append(body)
        hero_box.append(panes)
    else:
        hero_box.append(body)
    return hero_box


def page_hero_actions(*buttons: Gtk.Widget | None) -> Gtk.Widget:
    """Right-aligned action row that sits above a page hero (Refresh, …)."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.add_css_class("fu-page-hero-actions")
    row.set_hexpand(True)
    spacer = Gtk.Box()
    spacer.set_hexpand(True)
    row.append(spacer)
    for btn in buttons:
        if btn is None:
            continue
        btn.set_valign(Gtk.Align.CENTER)
        row.append(btn)
    return row


def page_callout(
    title: str,
    subtitle: str,
    *buttons: Gtk.Widget,
) -> Gtk.Widget:
    """Dashed info strip (restore point / tip / path)."""
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    row.add_css_class("fu-page-callout")
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    texts.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0.0)
    t.add_css_class("fu-page-card-title")
    texts.append(t)
    s = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
    s.add_css_class("fu-page-card-sub")
    texts.append(s)
    row.append(texts)
    for btn in buttons:
        row.append(btn)
    return row


def page_section_label(text: str) -> Gtk.Widget:
    lab = Gtk.Label(label=text, xalign=0.0)
    lab.add_css_class("fu-section-title")
    return lab


def wide_clamp(maximum_size: int = CONTENT_MAX, *, side_pad: int = PAGE_SIDE_PAD) -> Adw.Clamp:
    """Full-width clamp — no mid-size tightening, so pages match the Apps layout."""
    clamp = Adw.Clamp(maximum_size=maximum_size)
    clamp.set_hexpand(True)
    if side_pad:
        clamp.set_margin_start(side_pad)
        clamp.set_margin_end(side_pad)
    try:
        clamp.set_tightening_threshold(maximum_size)
    except Exception:  # noqa: BLE001
        pass
    return clamp


def page_scroll_body(
    *, spacing: int = 14, side_pad: int = PAGE_SIDE_PAD
) -> tuple[Gtk.ScrolledWindow, Adw.Clamp, Gtk.Box]:
    """Standard wide scroll + clamp + vertical column for shell pages."""
    scrolled = Gtk.ScrolledWindow()
    scrolled.add_css_class("fu-page-scroll")
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    try:
        scrolled.set_kinetic_scrolling(True)
        scrolled.set_overlay_scrolling(True)
        scrolled.set_propagate_natural_height(False)
        scrolled.set_propagate_natural_width(False)
    except Exception:  # noqa: BLE001
        pass
    col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    col.set_hexpand(True)
    col.set_margin_top(4)
    col.set_margin_bottom(8)
    if side_pad:
        clamp = wide_clamp(side_pad=side_pad)
        clamp.set_child(col)
        scrolled.set_child(clamp)
        return scrolled, clamp, col
    # Same as Apps: no Clamp, so the body shares the page-frame inset with the hero.
    scrolled.set_child(col)
    return scrolled, col, col


def page_card_grid(
    cards: list[Gtk.Widget], columns: int = 3, *, fill: bool = False
) -> Gtk.Widget:
    """Fixed N-column grid so Overview / Health cards share width and side inset."""
    grid = Gtk.Grid()
    grid.add_css_class("fu-overview-flow")
    grid.set_column_spacing(12)
    grid.set_row_spacing(12)
    grid.set_column_homogeneous(True)
    grid.set_hexpand(True)
    grid.set_halign(Gtk.Align.FILL)
    if fill:
        grid.set_row_homogeneous(True)
        grid.set_vexpand(True)
        grid.set_valign(Gtk.Align.FILL)
    else:
        grid.set_valign(Gtk.Align.START)
    for i, card in enumerate(cards):
        card.set_hexpand(True)
        card.set_halign(Gtk.Align.FILL)
        card.set_valign(Gtk.Align.FILL)
        if fill:
            card.set_vexpand(True)
        grid.attach(card, i % columns, i // columns, 1, 1)
    return grid


def page_frame() -> Gtk.Box:
    """Same outer inset as Apps: one pad for the hero and the page body."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.set_hexpand(True)
    box.set_vexpand(True)
    box.add_css_class("fu-padded-page")
    box.set_margin_start(PAGE_SIDE_PAD)
    box.set_margin_end(PAGE_SIDE_PAD)
    return box


def pin_page_footer(widget: Gtk.Widget) -> Gtk.Widget:
    """Keep apply / install / save bars on screen below the scroller."""
    widget.set_hexpand(True)
    widget.set_vexpand(False)
    widget.set_valign(Gtk.Align.END)
    return widget


def page_chrome_box(*, side_pad: int = PAGE_SIDE_PAD) -> Gtk.Box:
    """Hero/callout band pinned above the scroller so gradients don't recompose on every frame."""
    chrome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    chrome.add_css_class("fu-page-chrome")
    chrome.set_hexpand(True)
    if side_pad:
        chrome.set_margin_start(side_pad)
        chrome.set_margin_end(side_pad)
    return chrome


def build_checking_content(
    status: str = "Checking for updates…",
) -> tuple[Gtk.Widget, Callable[[str], None], Callable[[], None]]:
    """Full-window checking splash used as the first home view."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_vexpand(True)
    outer.set_hexpand(True)

    center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    center.add_css_class("fu-checking")
    center.set_halign(Gtk.Align.CENTER)
    center.set_valign(Gtk.Align.CENTER)
    center.set_vexpand(True)
    center.set_hexpand(True)

    icon = spinning_icon("view-refresh-symbolic", 72)
    icon.set_halign(Gtk.Align.CENTER)
    center.append(icon)

    title = Gtk.Label(label="UrStack")
    title.add_css_class("fu-checking-title")
    title.set_halign(Gtk.Align.CENTER)
    center.append(title)

    status_lbl = Gtk.Label(label=status, wrap=True)
    status_lbl.add_css_class("fu-checking-status")
    status_lbl.set_halign(Gtk.Align.CENTER)
    status_lbl.set_justify(Gtk.Justification.CENTER)
    center.append(status_lbl)

    bar = Gtk.ProgressBar()
    bar.set_size_request(280, -1)
    bar.pulse()
    center.append(bar)

    outer.append(center)
    pulse_state = {"alive": True, "id": 0}

    def _pulse() -> bool:
        if not pulse_state["alive"]:
            return False
        bar.pulse()
        return True

    pulse_state["id"] = GLib.timeout_add(120, _pulse)

    def set_status(text: str) -> None:
        status_lbl.set_text(text)

    def stop() -> None:
        pulse_state["alive"] = False
        if pulse_state["id"]:
            try:
                GLib.source_remove(pulse_state["id"])
            except Exception:  # noqa: BLE001
                pass
            pulse_state["id"] = 0

    return outer, set_status, stop


def mk_btn(
    label: str,
    css: str | None = None,
    icon: str | None = None,
    *,
    icon_size: int = 22,
) -> Gtk.Button:
    btn = Gtk.Button()
    if icon:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        if label:
            lab = Gtk.Label(label=label)
            box.append(lab)
        img = Gtk.Image.new_from_icon_name(icon)
        img.set_pixel_size(icon_size)
        img.set_valign(Gtk.Align.CENTER)
        box.append(img)
        btn.set_child(box)
    else:
        btn.set_label(label)
    if css:
        for c in css.split():
            btn.add_css_class(c)
    return btn


def mono_label(text: str) -> Gtk.Label:
    lab = Gtk.Label(label=text, xalign=0.0, wrap=True, selectable=True)
    lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.add_css_class("fu-mono")
    lab.set_hexpand(True)
    return lab


def section_card(section: Section) -> Gtk.Widget:
    """Legacy card (mono dump) — prefer section_expander for the hub."""
    clamp = wide_clamp()
    clamp.add_css_class("fu-section-card")

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    title = Gtk.Label(label=section.title, xalign=0.0)
    title.add_css_class("fu-section-title")
    box.append(title)

    frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    frame.add_css_class("card")
    frame.set_margin_top(2)

    inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    inner.set_margin_top(12)
    inner.set_margin_bottom(12)
    inner.set_margin_start(14)
    inner.set_margin_end(14)
    inner.append(mono_label(section.body))
    frame.append(inner)
    box.append(frame)
    clamp.set_child(box)
    return clamp


def section_icon_name(title: str) -> str:
    t = title.lower()
    if "dnf" in t or "rpm" in t:
        return "package-x-generic-symbolic"
    if "flatpak" in t:
        return "application-x-addon-symbolic"
    if "snap" in t:
        return "media-floppy-symbolic"
    if "firmware" in t or "fwupd" in t:
        return "computer-symbolic"
    if "toolbx" in t or "distrobox" in t or "toolbox" in t:
        return "container-terminal-symbolic"
    if "npm" in t or "node" in t:
        return "text-x-script-symbolic"
    if "pip" in t or "python" in t:
        return "text-x-python-symbolic"
    if "rust" in t or "cargo" in t:
        return "applications-engineering-symbolic"
    if "cursor" in t or "claude" in t or "supabase" in t:
        return "applications-development-symbolic"
    if "jetbrains" in t:
        return "preferences-desktop-symbolic"
    if "appimage" in t:
        return "application-x-executable-symbolic"
    if "preflight" in t:
        return "dialog-warning-symbolic"
    if "changelog" in t:
        return "view-list-symbolic"
    return "view-refresh-symbolic"


def section_expander(section: Section) -> Adw.ExpanderRow:
    # Normalize body and drop leftover === header lines
    body = section.body.replace("\\n", "\n")
    lines = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or re.match(r"^=== .+ ===$", s):
            continue
        lines.append(s)

    title = re.sub(r"^===|===$", "", section.title).strip()
    # "Flatpak (1 update(s))" → title Flatpak, count from body
    base_title = re.sub(r"\s*\(\d+.*?\)\s*$", "", title).strip() or title

    if section.kind == "update":
        subtitle = f"{len(lines)} update{'s' if len(lines) != 1 else ''}"
    elif section.kind == "advisory":
        subtitle = f"{len(lines)} note{'s' if len(lines) != 1 else ''}"
    else:
        subtitle = "Info"

    row = Adw.ExpanderRow(title=base_title, subtitle=subtitle)
    # Expand small update lists by default
    if section.kind == "update":
        row.set_expanded(len(lines) <= 12)
    icon = Gtk.Image.new_from_icon_name(section_icon_name(title))
    icon.set_pixel_size(22)
    row.add_prefix(icon)

    if section.kind == "update":
        badge = Gtk.Label(label="Ready")
        badge.add_css_class("fu-badge")
        badge.add_css_class("fu-badge-warn")
        badge.set_valign(Gtk.Align.CENTER)
        row.add_suffix(badge)

    if not lines:
        empty = Adw.ActionRow(title="No details")
        row.add_row(empty)
        return row

    for ln in lines[:100]:
        pretty, detail = pretty_package_line(ln)
        item = Adw.ActionRow(title=pretty or ln)
        if detail:
            item.set_subtitle(detail)
        if section.kind == "advisory":
            tip = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
            tip.set_pixel_size(14)
            item.add_suffix(tip)
        else:
            pkg = Gtk.Image.new_from_icon_name("package-x-generic-symbolic")
            pkg.set_pixel_size(14)
            item.add_prefix(pkg)
        row.add_row(item)
    if len(lines) > 100:
        more = Adw.ActionRow(title=f"…and {len(lines) - 100} more")
        row.add_row(more)
    return row


def build_uptodate_content() -> Gtk.Widget:
    """Centered success state when nothing needs applying (legacy/standalone)."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_vexpand(True)
    outer.set_hexpand(True)
    center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    center.add_css_class("fu-uptodate")
    center.set_halign(Gtk.Align.CENTER)
    center.set_valign(Gtk.Align.CENTER)
    center.set_vexpand(True)
    center.set_hexpand(True)
    icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
    icon.set_pixel_size(128)
    icon.add_css_class("fu-uptodate-icon")
    icon.set_halign(Gtk.Align.CENTER)
    center.append(icon)
    title = Gtk.Label(label="All up to date")
    title.add_css_class("fu-uptodate-title")
    title.set_halign(Gtk.Align.CENTER)
    center.append(title)
    sub = Gtk.Label(
        label="Every enabled source is current. Refresh anytime to check again.",
        wrap=True,
    )
    sub.add_css_class("fu-uptodate-sub")
    sub.set_halign(Gtk.Align.CENTER)
    sub.set_justify(Gtk.Justification.CENTER)
    center.append(sub)
    outer.append(center)
    return outer


def _overview_stat_card(
    title: str,
    subtitle: str,
    icon_name: str,
    action: str,
    on_action: Callable[[str], None],
    *,
    badge: str = "",
    badge_ok: bool = False,
    badge_warn: bool = False,
    blurb: str = "",
    lines: list[str] | None = None,
    spinning: bool = False,
) -> Gtk.Widget:
    """Overview info card for the 3×3 section grid."""
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    card.add_css_class("fu-overview-card")
    if badge_warn:
        card.add_css_class("fu-overview-card-warn")
    elif badge_ok:
        card.add_css_class("fu-overview-card-ok")
    card.set_hexpand(True)
    card.set_vexpand(True)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    head.set_hexpand(True)

    if spinning:
        # Same rotating refresh spinner for every scanning card
        icon = spinning_icon("view-refresh-symbolic", 34)
    else:
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(34)
    icon.add_css_class("fu-overview-card-icon")
    icon.set_valign(Gtk.Align.START)
    head.append(icon)

    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    titles.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0.0)
    t.add_css_class("fu-overview-card-title")
    t.set_ellipsize(Pango.EllipsizeMode.END)
    t.set_single_line_mode(True)
    titles.append(t)
    s = Gtk.Label(label=subtitle, xalign=0.0, wrap=True)
    s.add_css_class("fu-overview-card-status")
    s.set_lines(2)
    s.set_ellipsize(Pango.EllipsizeMode.END)
    titles.append(s)
    head.append(titles)

    if badge:
        bdg = Gtk.Label(label=badge)
        bdg.add_css_class("fu-badge")
        if badge_ok:
            bdg.add_css_class("fu-badge-ok")
        elif badge_warn:
            bdg.add_css_class("fu-badge-warn")
        bdg.set_valign(Gtk.Align.START)
        bdg.set_margin_top(2)
        head.append(bdg)
    card.append(head)

    if blurb:
        b = Gtk.Label(label=blurb, xalign=0.0, wrap=True)
        b.add_css_class("fu-overview-card-blurb")
        b.set_lines(2)
        b.set_ellipsize(Pango.EllipsizeMode.END)
        card.append(b)

    padded = ([ln for ln in (lines or []) if ln] + ["", "", ""])[:3]
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    body.set_vexpand(True)
    body.set_valign(Gtk.Align.FILL)
    details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    details.set_margin_top(10)
    for line in padded:
        row = Gtk.Label(label=f"· {line}" if line else " ", xalign=0.0)
        row.add_css_class("fu-overview-card-line")
        row.set_lines(1)
        row.set_ellipsize(Pango.EllipsizeMode.END)
        if not line:
            row.set_opacity(0)
        details.append(row)
    body.append(details)
    card.append(body)

    foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    foot.set_margin_top(12)
    foot.set_hexpand(True)
    css = "suggested-action pill fu-primary" if badge_warn else "pill fu-secondary"
    go = mk_btn(
        "Open",
        css,
        pick_icon("go-next-symbolic", "pan-end-symbolic", "document-open-symbolic"),
    )
    go.set_hexpand(True)
    go.connect("clicked", lambda *_: on_action(action))
    foot.append(go)
    card.append(foot)
    return card


def _overview_apps_snapshot(status_file: str) -> tuple[int, int, list[str]]:
    rows = _load_catalog_rows(Path(status_file)) if status_file else []
    installed = sum(1 for r in rows if r.get("installed") == "1")
    available = sum(1 for r in rows if r.get("installed") != "1")
    by_cat: dict[str, int] = {}
    methods = {"flatpak": 0, "dnf": 0, "other": 0}
    for r in rows:
        if r.get("installed") != "1":
            continue
        cat = (r.get("category") or "Other").strip() or "Other"
        by_cat[cat] = by_cat.get(cat, 0) + 1
        m = (r.get("method") or "").lower()
        if "flatpak" in m:
            methods["flatpak"] += 1
        elif m == "dnf":
            methods["dnf"] += 1
        else:
            methods["other"] += 1
    top = sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:3]
    lines: list[str] = []
    if top:
        lines.append("Installed by category: " + ", ".join(f"{n} ({c})" for c, n in top))
    bits = []
    if methods["flatpak"]:
        bits.append(f"{methods['flatpak']} Flatpak")
    if methods["dnf"]:
        bits.append(f"{methods['dnf']} DNF")
    if methods["other"]:
        bits.append(f"{methods['other']} vendor/other")
    if bits:
        lines.append("Sources in use: " + " · ".join(bits))
    if available:
        lines.append(f"{available} catalog apps available to install")
    return installed, available, lines


def _overview_health_snapshot(status_file: str) -> tuple[str, str, bool, list[str]]:
    """Return (score_or_dash, subtitle, needs_attention, detail_lines)."""
    path = Path(status_file) if status_file else Path()
    if not path.is_file() or path.stat().st_size == 0:
        return (
            "—",
            "Waiting for first health scan…",
            False,
            ["Checks kernels, caches, Flatpak leftovers, codecs, power, and tuneables."],
        )
    rows = _load_health_rows(path)
    if not rows:
        return (
            "—",
            "Waiting for first health scan…",
            False,
            ["Checks kernels, caches, Flatpak leftovers, codecs, power, and tuneables."],
        )
    attention, optional = _health_problem_rows(rows)
    ok_n = sum(1 for r in rows if r.get("severity") == "ok")
    lines: list[str] = []
    rp_id, rp_created = _health_latest_restore_point()
    if rp_id:
        lines.append(f"Restore point: {rp_created or rp_id}")
    else:
        lines.append("No health restore point yet — one is created before applying fixes.")

    if not attention:
        opt = len(optional)
        if opt:
            lines.append(f"{ok_n} checks clear · {opt} optional tweak(s)")
            for r in optional[:3]:
                title = (r.get("title") or r.get("id") or "Check").strip()
                lines.append(title)
            if opt > 3:
                lines.append(f"+{opt - 3} more optional tweak(s)")
            return (
                "100",
                f"{ok_n} checks clear · {opt} optional.",
                False,
                lines,
            )
        lines.append(f"{ok_n} checks clear · nothing to apply")
        return "100", f"{ok_n} checks clear · nothing urgent.", False, lines

    score = max(55, 100 - len(attention) * 12)
    for r in (attention + optional)[:3]:
        title = (r.get("title") or r.get("id") or "Check").strip()
        detail = (r.get("detail") or "").strip()
        lines.append(f"{title}" + (f" — {detail[:70]}" if detail else ""))
    if len(attention) + len(optional) > 3:
        lines.append(f"+{len(attention) + len(optional) - 3} more recommendation(s)")
    sub = f"{len(attention)} need attention · {len(optional)} optional fixes"
    return str(score), sub, True, lines


def _overview_update_lines(update_secs: list[Section]) -> list[str]:
    lines: list[str] = []
    for sec in update_secs[:4]:
        n = sum(1 for ln in sec.body.splitlines() if ln.strip() and not ln.strip().startswith("#"))
        if n:
            lines.append(f"{sec.title}: ~{n} item(s)")
        else:
            lines.append(sec.title)
    if len(update_secs) > 4:
        lines.append(f"+{len(update_secs) - 4} more source(s)")
    return lines


def _overview_last_run(runs_dir: str) -> tuple[str, list[str]]:
    root = Path(runs_dir) if runs_dir else Path()
    if not root.is_dir():
        return "No apply runs recorded yet.", ["Apply/update sessions land here with a summary."]
    dirs = sorted(
        (p for p in root.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not dirs:
        return "No apply runs recorded yet.", ["Apply/update sessions land here with a summary."]
    newest = dirs[0]
    lines = [f"Latest folder: {newest.name}"]
    summary = newest / "summary.txt"
    headline = newest.name
    if summary.is_file():
        text_lines = summary.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        if text_lines:
            headline = text_lines[0][:120]
            for extra in text_lines[1:3]:
                if extra.strip():
                    lines.append(extra.strip()[:100])
    if len(dirs) > 1:
        lines.append(f"{len(dirs)} run folder(s) kept on disk")
    return headline, lines


def _last_backup_conf_path() -> Path:
    return Path.home() / ".config" / "urstack" / "last-backup.conf"


def _save_last_backup(dest: str, *, size_bytes: int | None = None) -> None:
    """Remember the most recent successful backup destination for Overview."""
    dest_path = Path(dest).expanduser()
    conf = _last_backup_conf_path()
    previous = read_config_map(conf) if conf.is_file() else {}
    conf.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    same_dest = previous.get("dest") == str(dest_path)
    created = previous.get("created", "").strip() if same_dest else ""
    if not created:
        created = datetime.now(UTC).astimezone().isoformat(timespec="seconds")
    lines = [
        f"dest={dest_path}",
        f"parent={dest_path.parent}",
        f"created={created}",
        f"name={dest_path.name}",
    ]
    size = size_bytes
    if size is None and same_dest:
        raw = previous.get("size_bytes", "").strip()
        if raw.isdigit():
            size = int(raw)
    if size is not None and size >= 0:
        lines.append(f"size_bytes={size}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _load_last_backup() -> dict[str, str]:
    path = _last_backup_conf_path()
    if not path.is_file():
        return {}
    return read_config_map(path)


def _pretty_backup_when(iso: str) -> str:
    if not iso:
        return ""
    try:
        from datetime import datetime

        raw = iso.strip()
        # Support trailing Z
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return iso[:19].replace("T", " ")


def format_byte_size(total: int) -> str:
    """Human size for Overview (1024-based). Empty string for non-positive."""
    if total <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(total)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return ""


# dest -> (dir mtime_ns, label). Filled off the UI thread; hit on later paints.
_BACKUP_SIZE_CACHE: dict[str, tuple[int, str]] = {}
_BACKUP_SIZE_INFLIGHT: set[str] = set()
_BACKUP_SIZE_READY: list[Callable[[], bool]] = []


def _walk_dir_size(path: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def _dir_size_label(path: Path) -> str:
    """Return a cached size, or schedule a walk and return '' for this paint.

    Walking a backup of tens of thousands of files on the GTK thread froze
    Overview for ~1 s; the walk now happens in a daemon thread.
    """
    if not path.is_dir():
        return ""
    key = str(path)
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return ""
    cached = _BACKUP_SIZE_CACHE.get(key)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    if key in _BACKUP_SIZE_INFLIGHT:
        return ""
    _BACKUP_SIZE_INFLIGHT.add(key)

    def work() -> None:
        try:
            total = _walk_dir_size(path)
            label = format_byte_size(total)
            try:
                now_mtime = path.stat().st_mtime_ns
            except OSError:
                now_mtime = mtime_ns
            _BACKUP_SIZE_CACHE[key] = (now_mtime, label)
            if total > 0:
                try:
                    prev = read_config_map(_last_backup_conf_path())
                    if prev.get("dest") == str(path):
                        _save_last_backup(str(path), size_bytes=total)
                except OSError:
                    pass
        finally:
            _BACKUP_SIZE_INFLIGHT.discard(key)
            for cb in _BACKUP_SIZE_READY:
                GLib.idle_add(cb)

    threading.Thread(target=work, daemon=True).start()
    return ""


def _discover_latest_backup_dir() -> Path | None:
    """Best-effort find of newest fedora-setup-* if no last-backup.conf yet."""
    roots: list[Path] = []
    meta = _load_last_backup()
    parent = (meta.get("parent") or "").strip()
    if parent:
        roots.append(Path(parent).expanduser())
    for candidate in (
        Path.home() / "Backups",
        Path.home() / "backup",
        Path.home() / "Documents" / "Backups",
    ):
        if candidate not in roots:
            roots.append(candidate)
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.glob("fedora-setup-*"):
                if p.is_dir() and (p / "manifests").is_dir():
                    found.append(p)
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return found[0]


def _overview_backup_snapshot() -> tuple[str, list[str]]:
    """Return (subtitle, detail lines) including last backup when known."""
    lines: list[str] = []
    meta = _load_last_backup()
    dest = Path(meta["dest"]).expanduser() if meta.get("dest") else None
    if dest is None:
        discovered = _discover_latest_backup_dir()
        if discovered is not None:
            dest = discovered
            meta = {
                "dest": str(discovered),
                "parent": str(discovered.parent),
                "name": discovered.name,
                "created": "",
            }

    if dest is not None:
        when = _pretty_backup_when(meta.get("created", ""))
        if not when and dest.is_dir():
            try:
                from datetime import datetime

                when = datetime.fromtimestamp(dest.stat().st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                )
            except OSError:
                when = ""
        if dest.is_dir():
            sub = f"Last backup {when}" if when else f"Last backup: {dest.name}"
            lines.append(str(dest))
            if when:
                lines.append(f"Finished: {when}")
            size = ""
            raw_bytes = (meta.get("size_bytes") or "").strip()
            if raw_bytes.isdigit():
                size = format_byte_size(int(raw_bytes))
            if not size:
                size = _dir_size_label(dest)
            if size:
                lines.append(f"Size: {size}")
        else:
            sub = "Last backup folder missing"
            lines.append(str(dest))
            lines.append("Folder not found — it may have been moved or deleted.")
            if when:
                lines.append(f"Was finished: {when}")
    else:
        sub = "No backup recorded yet"
        lines.append("Run Backup to create a dated fedora-setup folder.")

    extras = _load_extra_paths()
    if extras:
        lines.append(f"{len(extras)} custom path(s) included")
    opts = _backup_opts_path("backup")
    if opts.is_file():
        cfg = read_config_map(opts)
        on = [k for k, v in cfg.items() if v in {"1", "true", "yes", "on"}]
        if on:
            lines.append("Includes: " + ", ".join(on[:4]) + ("…" if len(on) > 4 else ""))
    return sub, lines[:4]


def _overview_settings_lines(config_file: str) -> list[str]:
    path = Path(config_file).expanduser() if config_file else Path()
    if not path.is_file():
        return ["Toggle DNF, Flatpak, firmware, and toolchain sources."]
    cfg = read_config_map(path)
    # Common enable_* style keys + known source toggles
    enabled: list[str] = []
    disabled: list[str] = []
    for key, val in sorted(cfg.items()):
        lk = key.lower()
        if not (
            lk.startswith("enable_")
            or lk.endswith("_updates")
            or lk in {"dnf", "flatpak", "firmware", "fwupd"}
        ):
            continue
        label = key.replace("enable_", "").replace("_", " ")
        if val in {"1", "true", "yes", "on"}:
            enabled.append(label)
        elif val in {"0", "false", "no", "off"}:
            disabled.append(label)
    lines: list[str] = []
    if enabled:
        lines.append("On: " + ", ".join(enabled[:6]) + ("…" if len(enabled) > 6 else ""))
    if disabled:
        lines.append("Off: " + ", ".join(disabled[:4]) + ("…" if len(disabled) > 4 else ""))
    if not lines:
        lines.append(f"Config: {path}")
    lines.append("Re-scan hardware/toolchains from Settings anytime.")
    return lines


def _overview_history_lines(log_file: str) -> list[str]:
    path = Path(log_file).expanduser() if log_file else Path()
    if not path.is_file():
        return ["Command history for checks, applies, and installs."]
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ["Could not read the history log."]
    chunks = [c.strip() for c in re.split(r"\n(?=---+)", text) if c.strip()]
    if not chunks:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return (lines[-3:] if lines else ["Log is empty."])
    last = chunks[-1].splitlines()
    out = [ln.strip()[:110] for ln in last[:3] if ln.strip()]
    out.append(f"{len(chunks)} history entries")
    return out or ["Log is empty."]


def build_overview_content(
    *,
    raw: str,
    has_updates: bool,
    status_file: str = "",
    health_status_file: str = "",
    runs_dir: str = "",
    log_file: str = "",
    config_file: str = "",
    checking: bool = False,
    checking_updates: bool = False,
    checking_health: bool = False,
    on_action: Callable[[str], None],
    on_refresh: Callable[[], None] | None = None,
) -> Gtk.Widget:
    """Landing page: workstation snapshot + shortcuts into the rest of the shell."""
    outer = page_frame()

    scanning_now = bool(checking or checking_updates or checking_health)
    refresh_top = None
    if on_refresh is not None:
        refresh_top = mk_btn("Refresh", "flat", "view-refresh-symbolic")
        refresh_top.set_valign(Gtk.Align.CENTER)
        refresh_top.set_sensitive(not scanning_now)
        refresh_top.connect("clicked", lambda *_: on_refresh())
    hero_head = dict(
        heading="Overview",
        heading_sub="Your Fedora workstation at a glance — jump into updates, apps, or health.",
        icon_name=page_icon("overview"),
        heading_trailing=refresh_top,
    )

    sections = parse_sections(raw)
    update_secs = [s for s in sections if s.kind == "update" and s.title != "Overview"]
    update_count = len(update_secs)
    installed_n, available_n, apps_lines = _overview_apps_snapshot(status_file)
    health_score, health_sub, health_warn, health_lines = _overview_health_snapshot(
        health_status_file
    )
    last_run, run_lines = _overview_last_run(runs_dir)
    checking = checking or checking_updates or checking_health

    scan_bits: list[str] = []
    if checking_updates:
        scan_bits.append("updates")
    if checking_health:
        scan_bits.append("health")
    scan_label = " & ".join(scan_bits) if scan_bits else "system"

    if checking:
        badge = Gtk.Label(label="Scanning")
        badge.add_css_class("fu-badge")
        badge.set_valign(Gtk.Align.CENTER)
        hero = page_hero(
            "…",
            "Scanning",
            f"Checking {scan_label}",
            "Updates and health run in the background — cards refresh as each scan finishes.",
            warn=False,
            trailing=badge,
            **hero_head,
        )
    elif has_updates and update_secs:
        badge = Gtk.Label(label="Action needed")
        badge.add_css_class("fu-badge")
        badge.add_css_class("fu-badge-warn")
        badge.set_valign(Gtk.Align.CENTER)
        hero = page_hero(
            str(update_count),
            "update source" + ("s" if update_count != 1 else ""),
            "Updates ready",
            "Review and apply from Updates when you're ready."
            + (
                f" Health score {health_score}."
                if health_score not in {"—", ""} and not checking_health
                else ""
            ),
            warn=True,
            trailing=badge,
            **hero_head,
        )
    else:
        badge = Gtk.Label(label="All clear")
        badge.add_css_class("fu-badge")
        badge.add_css_class("fu-badge-ok")
        badge.set_valign(Gtk.Align.CENTER)
        hero_sub = "No pending updates from enabled sources."
        if health_score == "100":
            hero_sub += " Health looks good too."
        elif health_warn:
            hero_sub += f" Health score {health_score} — open Health for recommendations."
        hero = page_hero(
            health_score if health_score not in {"—", ""} else "OK",
            "Workstation" if health_score in {"—", ""} else "health score",
            "Looking sharp" if not health_warn else "Mostly clear",
            hero_sub,
            warn=health_warn,
            ok=not health_warn,
            trailing=badge,
            **hero_head,
        )

    outer.append(hero)
    outer.append(page_section_label("Sections"))

    section_cards: list[Gtk.Widget] = []

    if checking_updates:
        upd_sub = "Scanning enabled sources…"
        upd_badge, upd_warn, upd_ok = "Scanning", False, False
        upd_lines = ["DNF, Flatpak, firmware, and any toolchains you enabled in Settings."]
    elif has_updates and update_secs:
        upd_sub = f"{update_count} source{'s' if update_count != 1 else ''} with updates"
        upd_badge, upd_warn, upd_ok = "Updates", True, False
        upd_lines = _overview_update_lines(update_secs)
    else:
        upd_sub = "Every enabled source is current"
        upd_badge, upd_warn, upd_ok = "Up to date", False, True
        upd_lines = ["Nothing pending — Refresh anytime to check again."]

    section_cards.append(
        _overview_stat_card(
            "Updates",
            upd_sub,
            page_icon("home"),
            "home",
            on_action,
            badge=upd_badge,
            badge_ok=upd_ok,
            badge_warn=upd_warn,
            blurb="Check every enabled package source, review what changed, then apply in one pass.",
            lines=upd_lines,
            spinning=checking_updates,
        )
    )

    if installed_n or available_n:
        apps_sub = f"{installed_n} installed · {available_n} available in catalog"
    else:
        apps_sub = "Browse Flatpak, DNF, and vendor apps"
    section_cards.append(
        _overview_stat_card(
            "Apps",
            apps_sub,
            page_icon("apps"),
            "apps",
            on_action,
            badge=str(installed_n) if installed_n else "Catalog",
            badge_ok=bool(installed_n),
            blurb="Curated catalog with real logos — desktop apps, CLIs, and vendor tools. Install Flatpak/DNF or open vendor links.",
            lines=apps_lines
            or ["Search, browse by category, and filter by availability or install method."],
        )
    )

    if checking_health:
        h_sub = "Scanning kernels, caches, Flatpak, power, and tuneables…"
        h_badge, h_ok, h_warn = "Scanning", False, False
        h_lines = ["Results appear here when the health scan finishes."]
    else:
        h_sub = health_sub
        h_badge = health_score if health_score != "—" else "Scan"
        h_ok = health_score == "100"
        h_warn = health_warn
        h_lines = health_lines

    section_cards.append(
        _overview_stat_card(
            "System Health",
            h_sub,
            page_icon("health"),
            "health",
            on_action,
            badge=h_badge,
            badge_ok=h_ok,
            badge_warn=h_warn,
            blurb="Curated fixes for this workstation — with an optional restore point before you apply.",
            lines=h_lines,
            spinning=checking_health,
        )
    )

    try:
        look_snap = look_engine.inspect_look()
        look_sub = look_snap.summary()
        look_lines = [
            f"{it.title}: {it.value}"
            for it in look_snap.items
            if it.value and it.value != "—"
        ][:3]
        look_badge = look_engine.desktop_label(look_snap.desktop)
    except Exception:  # noqa: BLE001
        look_sub = "Wallpaper, icons, widgets, and the rest of this desktop"
        look_lines = [
            "Save the look that is on screen, including custom icons and wallpaper.",
            "Open a theme archive (.tar / .zip) to install it for this user.",
        ]
        look_badge = "Look"
    section_cards.append(
        _overview_stat_card(
            "Look",
            look_sub,
            page_icon("look"),
            "look",
            on_action,
            badge=look_badge,
            badge_ok=True,
            blurb="Pack the current wallpaper, icons, widgets, and theme — or install a theme archive.",
            lines=look_lines
            or ["Export this desktop's look, or open a theme tar/zip to install it."],
        )
    )

    backup_sub, backup_lines = _overview_backup_snapshot()
    section_cards.append(
        _overview_stat_card(
            "Backup",
            backup_sub,
            page_icon("backup"),
            "backup",
            on_action,
            badge="Backup",
            badge_ok=backup_sub.startswith("Last backup"),
            badge_warn=backup_sub.startswith("Last backup folder missing"),
            blurb="Packages, configs, optional secrets, and project trees into a dated fedora-setup folder.",
            lines=backup_lines,
        )
    )

    section_cards.append(
        _overview_stat_card(
            "Restore",
            "Rebuild from a previous fedora-setup backup",
            page_icon("restore"),
            "restore",
            on_action,
            badge="Restore",
            blurb="Point at a backup folder to reinstall packages and bring configs back.",
            lines=[
                "Choose which layers to restore (packages, dotfiles, credentials, …).",
                "Safe to run after a fresh Fedora install or a bad change.",
            ],
        )
    )

    section_cards.append(
        _overview_stat_card(
            "Settings",
            "Sources, detection, and preferences",
            page_icon("settings"),
            "settings",
            on_action,
            badge="Config",
            blurb="Turn update sources on/off and re-detect toolchains installed on this machine.",
            lines=_overview_settings_lines(config_file),
        )
    )

    section_cards.append(
        _overview_stat_card(
            "History",
            "What UrStack has been doing",
            page_icon("log"),
            "log",
            on_action,
            badge="Log",
            blurb="Rolling log of checks, applies, installs, and other shell actions.",
            lines=_overview_history_lines(log_file),
        )
    )

    section_cards.append(
        _overview_stat_card(
            "Runs",
            last_run,
            page_icon("runs"),
            "runs",
            on_action,
            badge="Runs",
            blurb="Per-session folders from Apply — open a run to read its summary and logs.",
            lines=run_lines,
        )
    )
    scrolled = Gtk.ScrolledWindow()
    scrolled.add_css_class("fu-page-scroll")
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)
    scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    try:
        scrolled.set_kinetic_scrolling(True)
        scrolled.set_overlay_scrolling(True)
        scrolled.set_propagate_natural_height(False)
    except Exception:  # noqa: BLE001
        pass
    scrolled.set_child(page_card_grid(section_cards, columns=3, fill=True))
    outer.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    actions.add_css_class("fu-actions")
    actions.set_homogeneous(True)
    if has_updates and update_secs and not checking_updates:
        apply_btn = mk_btn(
            "Review updates",
            "suggested-action pill fu-primary",
            pick_icon(
                "software-update-available-symbolic",
                "document-open-symbolic",
                "go-next-symbolic",
            ),
        )
        apply_btn.connect("clicked", lambda *_: on_action("home"))
        actions.append(apply_btn)
    elif health_warn and not checking_health:
        health_pri = mk_btn(
            "Review health",
            "suggested-action pill fu-primary",
            pick_icon("document-open-symbolic", "folder-open-symbolic", page_icon("health")),
        )
        health_pri.connect("clicked", lambda *_: on_action("health"))
        actions.append(health_pri)
    else:
        apps_btn = mk_btn(
            "Browse apps",
            "suggested-action pill fu-primary",
            pick_icon("view-grid-symbolic", "view-app-grid-symbolic", page_icon("apps")),
        )
        apps_btn.connect("clicked", lambda *_: on_action("apps"))
        actions.append(apps_btn)
    if not (health_warn and not checking_health and not (has_updates and update_secs)):
        health_btn = mk_btn("Health", "flat fu-secondary", page_icon("health"))
        health_btn.connect("clicked", lambda *_: on_action("health"))
        actions.append(health_btn)
    outer.append(pin_page_footer(actions))
    return outer


def build_hub_content(
    *,
    raw: str,
    has_updates: bool,
    enable_backup: bool,
    on_action: Callable[[str], None],
    on_backup_visibility: Callable[[], bool] | None = None,
    show_nav_buttons: bool = True,
    on_refresh: Callable[[], None] | None = None,
    checking: bool = False,
) -> tuple[Gtk.Widget, Callable[[], None]]:
    """Updates home screen. Returns (widget, rebuild_nav_callback)."""
    sections = parse_sections(raw)
    update_secs = [s for s in sections if s.kind == "update" and s.title != "Overview"]
    update_count = len(update_secs)
    show_updates = bool(has_updates and update_secs)

    outer = page_frame()

    refresh_top = None
    if on_refresh is not None:
        refresh_top = mk_btn("Refresh", "flat", "view-refresh-symbolic")
        refresh_top.set_valign(Gtk.Align.CENTER)
        refresh_top.set_sensitive(not checking)
        refresh_top.connect("clicked", lambda *_: on_refresh())
    hero_head = dict(
        heading="Updates",
        heading_sub="Check every enabled source, review what changed, then apply in one pass.",
        icon_name=page_icon("home"),
        heading_trailing=refresh_top,
    )

    scrolled, _clamp, col = page_scroll_body(spacing=14, side_pad=0)

    if show_updates:
        badge = Gtk.Label(label="Action needed")
        badge.add_css_class("fu-badge")
        badge.add_css_class("fu-badge-warn")
        badge.set_valign(Gtk.Align.CENTER)
        hero = page_hero(
            str(update_count),
            "source" + ("s" if update_count != 1 else ""),
            "Updates ready",
            "Expand a source to review packages, then apply when you're happy.",
            warn=True,
            trailing=badge,
            **hero_head,
        )
        col.append(
            page_callout(
                "Tip",
                "Apply runs in this window. Privileged DNF / firmware steps use polkit once.",
            )
        )
        col.append(page_section_label("Sources with updates"))
        group = Adw.PreferencesGroup()
        for sec in update_secs:
            group.add(section_expander(sec))
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrap.add_css_class("fu-page-body")
        wrap.append(group)
        col.append(wrap)
    else:
        badge = Gtk.Label(label="All clear")
        badge.add_css_class("fu-badge")
        badge.add_css_class("fu-badge-ok")
        badge.set_valign(Gtk.Align.CENTER)
        hero = page_hero(
            "100",
            "up to date",
            "Looking sharp",
            "Every enabled source is current. Refresh anytime to check again.",
            warn=False,
            ok=True,
            trailing=badge,
            **hero_head,
        )
        col.append(
            page_callout(
                "Nothing to apply",
                "Install apps from Apps, tune the machine in Health, or refresh to re-check.",
            )
        )
        ok_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        ok_card.add_css_class("fu-page-card")
        ic = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        ic.set_pixel_size(64)
        ic.set_halign(Gtk.Align.START)
        ok_card.append(ic)
        ot = Gtk.Label(label="All up to date", xalign=0.0)
        ot.add_css_class("fu-page-card-title")
        ok_card.append(ot)
        os_ = Gtk.Label(
            label="DNF, Flatpak, firmware, and any enabled toolchains reported nothing pending.",
            xalign=0.0,
            wrap=True,
        )
        os_.add_css_class("fu-page-card-sub")
        ok_card.append(os_)
        col.append(ok_card)

    outer.append(hero)
    outer.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    actions.add_css_class("fu-actions")

    if show_updates:
        apply_label = (
            f"Apply updates ({update_count} source{'s' if update_count != 1 else ''})"
        )
        apply_btn = mk_btn(
            apply_label, "suggested-action pill fu-primary", "emblem-ok-symbolic"
        )
        apply_btn.set_hexpand(True)
        apply_btn.connect("clicked", lambda *_: on_action("apply"))
        actions.append(apply_btn)

    def rebuild_action_grid() -> None:
        return

    if show_nav_buttons:
        grid = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        grid.add_css_class("fu-action-grid")
        grid.set_homogeneous(True)

        backup_btn = mk_btn("Backup", "flat fu-secondary", page_icon("backup"))
        backup_btn.connect("clicked", lambda *_: on_action("backup"))
        restore_btn = mk_btn("Restore", "flat fu-secondary", page_icon("restore"))
        restore_btn.connect("clicked", lambda *_: on_action("restore"))
        settings_btn = mk_btn("Settings", "flat fu-secondary", page_icon("settings"))
        settings_btn.connect("clicked", lambda *_: on_action("settings"))
        apps_btn = mk_btn("Apps", "flat fu-secondary", page_icon("apps"))
        apps_btn.connect("clicked", lambda *_: on_action("apps"))
        log_btn = mk_btn("Log", "flat fu-secondary", page_icon("log"))
        log_btn.connect("clicked", lambda *_: on_action("log"))
        runs_btn = mk_btn("Runs", "flat fu-secondary", page_icon("runs"))
        runs_btn.connect("clicked", lambda *_: on_action("runs"))
        close_btn = mk_btn("Close", "flat fu-secondary", page_icon("close"))
        close_btn.connect("clicked", lambda *_: on_action("close"))

        def rebuild_action_grid() -> None:
            while grid.get_first_child() is not None:
                child = grid.get_first_child()
                grid.remove(child)
            for b in (backup_btn, restore_btn, apps_btn, settings_btn, log_btn, runs_btn, close_btn):
                parent = b.get_parent()
                if parent is not None:
                    parent.remove(b)
            for b in (backup_btn, restore_btn, apps_btn, settings_btn, log_btn, runs_btn, close_btn):
                grid.append(b)

        rebuild_action_grid()
        actions.append(grid)

    if actions.get_first_child() is not None:
        outer.append(pin_page_footer(actions))
    return outer, rebuild_action_grid


def build_shell_sidebar(
    on_action: Callable[[str], None],
    *,
    has_updates: bool = False,
    config_file: str = "",
) -> tuple[Gtk.Widget, Callable[[str], None], Callable[[bool], None]]:
    """Persistent left nav for the main shell. Returns (widget, set_active, set_has_updates)."""
    cfg_path = Path(config_file).expanduser() if config_file else default_config_path()
    collapsed = {"v": read_config_map(cfg_path).get("sidebar_collapsed", "0") == "1"}

    sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    sidebar.add_css_class("fu-shell-sidebar")
    sidebar.set_vexpand(True)

    brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    brand.add_css_class("fu-shell-sidebar-brand")
    brand.set_hexpand(True)
    logo = app_icon_image(36)
    logo.set_halign(Gtk.Align.START)
    logo.set_valign(Gtk.Align.CENTER)
    brand.append(logo)
    brand_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    brand_text.set_valign(Gtk.Align.CENTER)
    bt = Gtk.Label(label="UrStack", xalign=0.0)
    bt.add_css_class("fu-shell-sidebar-title")
    brand_text.append(bt)
    bs = Gtk.Label(label="Workstation", xalign=0.0)
    bs.add_css_class("fu-shell-sidebar-sub")
    brand_text.append(bs)
    brand.append(brand_text)
    sidebar.append(brand)

    sec_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    sec_host.add_css_class("fu-shell-nav-section-host")
    sec_host.set_size_request(-1, 32)
    sec = Gtk.Label(label="Navigate", xalign=0.0)
    sec.add_css_class("fu-shell-nav-section")
    sec_host.append(sec)
    sidebar.append(sec_host)

    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_vexpand(True)
    listbox = Gtk.ListBox()
    listbox.add_css_class("navigation-sidebar")
    listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
    scroll.set_child(listbox)
    sidebar.append(scroll)

    items: list[tuple[str, str, str]] = [
        # id, label, icon
        ("overview", "Overview", page_icon("overview")),
        ("home", "Updates", page_icon("home")),
        ("apps", "Apps", page_icon("apps")),
        ("health", "Health", page_icon("health")),
        ("look", "Look", page_icon("look")),
        ("backup", "Backup", page_icon("backup")),
        ("restore", "Restore", page_icon("restore")),
        ("settings", "Settings", page_icon("settings")),
        ("log", "History", page_icon("log")),
        ("runs", "Runs", page_icon("runs")),
        ("close", "Close", page_icon("close")),
    ]
    rows: dict[str, Gtk.ListBoxRow] = {}
    nav_labels: list[Gtk.Widget] = []
    nav_inners: list[Gtk.Box] = []
    suppress = {"v": False}

    for item_id, label, icon_name in items:
        row = Gtk.ListBoxRow()
        row.set_name(item_id)
        row.set_activatable(True)
        row.add_css_class("fu-shell-nav-row")
        row.set_size_request(-1, 36)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        inner.add_css_class("fu-shell-nav-inner")
        inner.set_valign(Gtk.Align.CENTER)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(18)
        icon.set_valign(Gtk.Align.CENTER)
        inner.append(icon)
        lab = Gtk.Label(label=label, xalign=0.0)
        lab.add_css_class("fu-shell-nav-label")
        lab.set_hexpand(True)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        inner.append(lab)
        row.set_child(inner)
        row.set_tooltip_text(label)
        listbox.append(row)
        rows[item_id] = row
        nav_labels.append(lab)
        nav_inners.append(inner)

    toggle = Gtk.Button()
    toggle.add_css_class("flat")
    toggle.add_css_class("fu-shell-sidebar-toggle")
    toggle.set_valign(Gtk.Align.CENTER)
    toggle_img = Gtk.Image()
    toggle.set_child(toggle_img)
    sidebar.append(toggle)

    def apply_collapsed() -> None:
        on = collapsed["v"]
        if on:
            sidebar.add_css_class("fu-shell-sidebar-collapsed")
        else:
            sidebar.remove_css_class("fu-shell-sidebar-collapsed")
        sidebar.set_size_request(52 if on else 212, -1)
        sidebar.set_hexpand(False)
        try:
            sidebar.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001
            pass
        brand_text.set_visible(not on)
        brand.set_spacing(0 if on else 10)
        brand.set_halign(Gtk.Align.CENTER if on else Gtk.Align.FILL)
        brand.set_hexpand(not on)
        logo.set_hexpand(False)
        logo.set_halign(Gtk.Align.CENTER if on else Gtk.Align.START)
        sec.set_visible(not on)
        for lab in nav_labels:
            lab.set_visible(not on)
        for inner in nav_inners:
            inner.set_halign(Gtk.Align.CENTER if on else Gtk.Align.FILL)
            inner.set_hexpand(not on)
        toggle_img.set_from_icon_name(
            "go-next-symbolic" if on else "go-previous-symbolic"
        )
        toggle.set_tooltip_text("Expand sidebar" if on else "Collapse sidebar")
        toggle.set_halign(Gtk.Align.CENTER if on else Gtk.Align.END)

    def on_toggle(*_a: object) -> None:
        collapsed["v"] = not collapsed["v"]
        apply_collapsed()
        try:
            write_config_map(
                cfg_path, {"sidebar_collapsed": "1" if collapsed["v"] else "0"}
            )
        except OSError:
            pass

    toggle.connect("clicked", on_toggle)
    apply_collapsed()

    def set_active(nav_id: str) -> None:
        # Apply is not a sidebar destination — map it to Updates
        if nav_id == "apply":
            nav_id = "home"
        row = rows.get(nav_id) or rows.get("overview") or rows.get("home")
        if row is None:
            return
        suppress["v"] = True
        listbox.select_row(row)
        suppress["v"] = False

    def set_has_updates(_enabled: bool) -> None:
        # Kept for API compatibility (Apply lives on the Updates page only)
        return

    def on_selected(_lb: Gtk.ListBox, _row: Gtk.ListBoxRow | None = None) -> None:
        if suppress["v"]:
            return
        row = listbox.get_selected_row()
        if row is None:
            return
        on_action(row.get_name() or "overview")

    listbox.connect("row-selected", on_selected)
    set_active("overview")
    return sidebar, set_active, set_has_updates


def configure_app_identity() -> None:
    """Make the process look like UrStack, not python3, to desktops/taskbars."""
    GLib.set_prgname("urstack")
    GLib.set_application_name("UrStack")
    try:
        # Must match StartupWMClass=urstack in the .desktop files
        Gdk.set_program_class("urstack")
    except Exception:  # noqa: BLE001
        pass
    try:
        Gtk.Window.set_default_icon_name("urstack")
    except Exception:  # noqa: BLE001
        pass


def tray_say(mode: str) -> None:
    """Tell the grey tray indicator what this window is doing. Never blocks."""
    line = (mode or "").strip()
    if not line:
        return
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    fifo = Path(base) / "urstack-tray.fifo"
    try:
        if not fifo.is_fifo():
            return
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, f"{line}\n".encode())
        finally:
            os.close(fd)
    except OSError:
        return


def run_app(app_id: str, build, *, unique: bool = True) -> int:
    configure_app_identity()
    Adw.init()
    load_css()
    apply_appearance(read_appearance())
    _ensure_theme_sync()
    flags = Gio.ApplicationFlags.FLAGS_NONE
    if not unique:
        flags = Gio.ApplicationFlags.NON_UNIQUE
    app = Adw.Application(application_id=app_id, flags=flags)
    state: dict = {"code": 0}
    built = {"v": False}

    def on_activate(application: Adw.Application) -> None:
        windows = application.get_windows()
        if windows:
            try:
                windows[0].present()
            except Exception:  # noqa: BLE001
                pass
            return
        if built["v"]:
            return
        built["v"] = True
        build(application, state)

    app.connect("activate", on_activate)
    app.run([])
    return int(state.get("code", 0))


# ── modes ──────────────────────────────────────────────────────────────────


def mode_hub(args: argparse.Namespace) -> int:
    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title)
        result = {"action": "close"}

        def finish(action: str) -> None:
            result["action"] = action
            win.close()

        outer, _rebuild = build_hub_content(
            raw=read_text(args.file, None),
            has_updates=bool(args.has_updates),
            enable_backup=bool(getattr(args, "enable_backup", 0)),
            on_action=finish,
        )
        wrap_shell(win, "", outer, None)

        def on_close(*_a: object) -> bool:
            print(result["action"], flush=True)
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.maximize()
        win.present()

    return run_app("com.local.urstack.hub", build)


def build_checklist_content(
    items: list[Item],
    *,
    heading: str = "Choose what to update",
    subheading: str = "Selected sections will run in order.",
    ok_label: str = "Apply",
    on_confirm: Callable[[list[str]], None] | None = None,
    on_cancel: Callable[[], None] | None = None,
) -> Gtk.Widget:
    """Reusable multi-select list for apply sections — Apps-page polish."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_hexpand(True)
    outer.set_vexpand(True)

    outer.append(
        page_toolbar(
            heading,
            subheading,
            icon_name="emblem-ok-symbolic",
        )
    )

    hint = Gtk.Label(
        label=f"{len(items)} source{'s' if len(items) != 1 else ''} available",
        xalign=0.0,
    )
    hint.add_css_class("dim-label")
    hint.add_css_class("caption")
    hint.set_margin_start(16)
    hint.set_margin_end(16)
    hint.set_margin_bottom(4)
    outer.append(hint)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.set_hexpand(True)
    scrolled.set_margin_start(16)
    scrolled.set_margin_end(16)
    scrolled.set_margin_top(4)

    clamp = wide_clamp()
    list_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

    sec = Gtk.Label(label="Sources to apply", xalign=0.0)
    sec.add_css_class("fu-section-title")
    list_col.append(sec)

    listbox = Gtk.ListBox()
    listbox.set_selection_mode(Gtk.SelectionMode.NONE)
    listbox.add_css_class("boxed-list")
    listbox.add_css_class("fu-check-row")
    checks: list[tuple[Item, Gtk.CheckButton]] = []

    icons = {
        "dnf": "package-x-generic-symbolic",
        "snap": "media-floppy-symbolic",
        "fw": "drive-harddisk-symbolic",
        "flatpak": "application-x-addon-symbolic",
        "toolbox": "utilities-terminal-symbolic",
        "npm": "text-x-script-symbolic",
        "npm_user": "text-x-script-symbolic",
        "pip": "text-x-script-symbolic",
        "pipx": "text-x-script-symbolic",
        "rust": "applications-engineering-symbolic",
        "cargo": "applications-engineering-symbolic",
        "node": "text-x-script-symbolic",
        "cursor": "preferences-desktop-display-symbolic",
        "claude": "computer-symbolic",
        "supabase": "network-server-symbolic",
    }

    for it in items:
        row = Adw.ActionRow(title=it.label, subtitle=it.item_id)
        row.set_activatable(True)
        icon_name = icons.get(it.item_id, "view-list-symbolic")
        row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
        cb = Gtk.CheckButton()
        cb.set_active(it.checked)
        cb.set_valign(Gtk.Align.CENTER)
        row.add_suffix(cb)
        row.set_activatable_widget(cb)

        def toggle(_row: Adw.ActionRow, button: Gtk.CheckButton = cb) -> None:
            button.set_active(not button.get_active())

        row.connect("activated", toggle)
        listbox.append(row)
        checks.append((it, cb))

    list_col.append(listbox)
    clamp.set_child(list_col)
    scrolled.set_child(clamp)
    outer.append(scrolled)

    sel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    sel_row.set_margin_start(16)
    sel_row.set_margin_end(16)
    sel_row.set_margin_top(8)
    sel_row.set_margin_bottom(4)
    all_btn = mk_btn("Select all", "flat", "edit-select-all-symbolic")
    none_btn = mk_btn("Clear", "flat", "edit-clear-all-symbolic")
    all_btn.connect("clicked", lambda *_: [c.set_active(True) for _, c in checks])
    none_btn.connect("clicked", lambda *_: [c.set_active(False) for _, c in checks])
    sel_row.append(all_btn)
    sel_row.append(none_btn)
    sel_hint = Gtk.Label(label="Choose sources, then apply", xalign=0.0)
    sel_hint.add_css_class("dim-label")
    sel_hint.add_css_class("caption")
    sel_hint.set_hexpand(True)
    sel_hint.set_halign(Gtk.Align.END)
    sel_row.append(sel_hint)
    outer.append(sel_row)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    actions.add_css_class("fu-actions")
    go_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    go_row.set_homogeneous(True)
    cancel_btn = mk_btn("Cancel", "flat fu-secondary")
    ok_btn = mk_btn(ok_label, "suggested-action pill fu-primary", "emblem-ok-symbolic")

    def do_cancel(*_a: object) -> None:
        if on_cancel is not None:
            on_cancel()

    def do_confirm(*_a: object) -> None:
        selected = [it.item_id for it, cb in checks if cb.get_active()]
        if on_confirm is not None:
            on_confirm(selected)

    cancel_btn.connect("clicked", do_cancel)
    ok_btn.connect("clicked", do_confirm)
    go_row.append(cancel_btn)
    go_row.append(ok_btn)
    actions.append(go_row)
    outer.append(pin_page_footer(actions))
    return outer


def mode_checklist(args: argparse.Namespace) -> int:
    items = parse_items_file(args.items_file)
    if not items:
        return 1

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title, 720, 640)
        result: dict = {"ok": False, "selected": []}

        def on_confirm(selected: list[str]) -> None:
            result["ok"] = True
            result["selected"] = selected
            win.close()

        def on_cancel() -> None:
            result["ok"] = False
            win.close()

        outer = build_checklist_content(
            items,
            heading=args.text or "Choose what to update",
            ok_label=args.ok_label,
            on_confirm=on_confirm,
            on_cancel=on_cancel,
        )
        wrap_shell(win, args.title, outer, "Select sections")

        def on_close(*_a: object) -> bool:
            if result["ok"]:
                print("|".join(result["selected"]), flush=True)
                state["code"] = 0
            else:
                state["code"] = 1
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.checklist", build)


def mode_radio(args: argparse.Namespace) -> int:
    items = parse_items_file(args.items_file)
    if not items:
        return 1

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title, 640, 520)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        if args.text:
            lab = Gtk.Label(label=args.text, wrap=True, xalign=0.0)
            lab.add_css_class("title-4")
            lab.add_css_class("fu-body")
            outer.append(lab)

        clamp = Adw.Clamp(maximum_size=560)
        clamp.set_margin_top(12)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)

        group: Gtk.CheckButton | None = None
        radios: list[tuple[Item, Gtk.CheckButton]] = []

        for it in items:
            row = Adw.ActionRow(title=it.label)
            rb = Gtk.CheckButton()
            if group is None:
                group = rb
            else:
                rb.set_group(group)
            rb.set_active(it.checked)
            rb.set_valign(Gtk.Align.CENTER)
            row.add_prefix(rb)
            row.set_activatable_widget(rb)
            listbox.append(row)
            radios.append((it, rb))

        clamp.set_child(listbox)
        outer.append(clamp)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.add_css_class("fu-actions")
        actions.set_homogeneous(True)
        result = {"ok": False}

        def finish(ok: bool) -> None:
            result["ok"] = ok
            win.close()

        cancel_btn = mk_btn("Cancel", "flat")
        cancel_btn.connect("clicked", lambda *_: finish(False))
        ok_btn = mk_btn(args.ok_label, "suggested-action pill", "emblem-ok-symbolic")
        ok_btn.connect("clicked", lambda *_: finish(True))
        actions.append(cancel_btn)
        actions.append(ok_btn)
        outer.append(pin_page_footer(actions))
        wrap_shell(win, args.title, outer)

        def on_close(*_a: object) -> bool:
            if result["ok"]:
                for it, rb in radios:
                    if rb.get_active():
                        print(it.item_id, flush=True)
                        break
                state["code"] = 0
            else:
                state["code"] = 1
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.radio", build)


def mode_text(args: argparse.Namespace) -> int:
    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        body = read_text(args.file, args.text) or "(Empty)"

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(8)
        scrolled.set_margin_start(16)
        scrolled.set_margin_end(16)
        scrolled.set_margin_bottom(4)

        clamp = Adw.Clamp(maximum_size=860)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        inner.set_margin_top(14)
        inner.set_margin_bottom(14)
        inner.set_margin_start(16)
        inner.set_margin_end(16)
        inner.append(mono_label(body))
        card.append(inner)
        clamp.set_child(card)
        scrolled.set_child(clamp)
        outer.append(scrolled)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.add_css_class("fu-actions")
        close_btn = mk_btn(args.ok_label or "Back", "suggested-action pill fu-primary")
        close_btn.set_hexpand(True)
        close_btn.connect("clicked", lambda *_: win.close())
        actions.append(close_btn)
        outer.append(pin_page_footer(actions))
        wrap_shell(win, args.title, outer, "History")

        def on_close(*_a: object) -> bool:
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.text", build)


def mode_message(args: argparse.Namespace) -> int:
    def build(app: Adw.Application, state: dict) -> None:
        # Large enough that multi-line status text is readable without a tiny scroll trap
        win = make_window(app, args.title, 640, 520)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        icon_name = "dialog-information-symbolic"
        if args.type == "error":
            icon_name = "dialog-error-symbolic"

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.set_margin_top(20)
        header.set_margin_start(24)
        header.set_margin_end(24)
        if args.type == "info" and APP_ICON.is_file():
            try:
                img = Gtk.Image()
                img.set_pixel_size(72)
                if hasattr(Gdk.Texture, "new_from_filename"):
                    img.set_from_paintable(Gdk.Texture.new_from_filename(str(APP_ICON)))
                header.append(img)
            except GLib.Error:
                ic = Gtk.Image.new_from_icon_name(icon_name)
                ic.set_pixel_size(64)
                header.append(ic)
        else:
            ic = Gtk.Image.new_from_icon_name(icon_name)
            ic.set_pixel_size(64)
            header.append(ic)
        title = Gtk.Label(label=args.title)
        title.add_css_class("title-2")
        title.set_halign(Gtk.Align.CENTER)
        header.append(title)
        outer.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_margin_start(24)
        scrolled.set_margin_end(24)
        scrolled.set_margin_top(8)
        body = Gtk.Label(label=args.text or "", wrap=True, xalign=0.0, selectable=True)
        body.add_css_class("fu-body")
        body.set_halign(Gtk.Align.CENTER)
        body.set_justify(Gtk.Justification.CENTER)
        scrolled.set_child(body)
        outer.append(scrolled)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.add_css_class("fu-actions")
        ok_btn = mk_btn("OK", "suggested-action pill fu-primary")
        ok_btn.set_hexpand(True)
        ok_btn.connect("clicked", lambda *_: win.close())
        actions.append(ok_btn)
        outer.append(pin_page_footer(actions))
        wrap_shell(win, args.title, outer)

        def on_close(*_a: object) -> bool:
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.message", build)


def mode_ask(args: argparse.Namespace) -> int:
    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title, 560, 400)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        status = Adw.StatusPage()
        status.set_icon_name("system-reboot-symbolic")
        status.set_title(args.title)
        status.set_description(args.text)
        status.set_vexpand(True)
        status.add_css_class("fu-status-page")
        outer.append(status)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.add_css_class("fu-actions")
        actions.set_homogeneous(True)
        result = {"yes": False}

        def finish(yes: bool) -> None:
            result["yes"] = yes
            win.close()

        is_reboot = "reboot" in (args.title or "").lower() or "reboot" in (args.text or "").lower()
        if is_reboot:
            no_btn = mk_btn("Not now", "flat fu-secondary")
            yes_btn = mk_btn("Reboot now", "suggested-action pill fu-primary", "system-reboot-symbolic")
        else:
            no_btn = mk_btn("No", "flat fu-secondary")
            yes_btn = mk_btn("Yes", "suggested-action pill fu-primary")
        no_btn.connect("clicked", lambda *_: finish(False))
        yes_btn.connect("clicked", lambda *_: finish(True))
        actions.append(no_btn)
        actions.append(yes_btn)
        outer.append(pin_page_footer(actions))
        wrap_shell(win, args.title, outer)

        def on_close(*_a: object) -> bool:
            state["code"] = 0 if result["yes"] else 1
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.ask", build)


def mode_folder(args: argparse.Namespace) -> int:
    def build(app: Adw.Application, state: dict) -> None:
        dialog = Gtk.FileDialog(title=args.title)
        start = Gio.File.new_for_path(os.path.expanduser(args.start or "~"))
        dialog.set_initial_folder(start)

        def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dialog.select_folder_finish(result)
                if folder:
                    print(folder.get_path() or "", flush=True)
                    state["code"] = 0
                else:
                    state["code"] = 1
            except GLib.Error:
                state["code"] = 1
            app.quit()

        win = make_window(app, args.title, 1, 1)
        win.set_decorated(False)
        win.set_opacity(0)
        win.present()
        dialog.select_folder(win, None, on_done)

    return run_app("com.local.urstack.folder", build)


def mode_progress(args: argparse.Namespace) -> int:
    """Zenity-compatible progress reader: lines '# text' or '0'..'100' from stdin.

    Compact mode (default for --pulsate / --compact): status + bar only — no empty log box.
    Detail mode: reveals a log once progress lines arrive (used while applying updates).
    """

    def build(app: Adw.Application, state: dict) -> None:
        compact = bool(getattr(args, "compact", False) or args.pulsate)
        if compact:
            # Splash-style check dialog — still large enough to read comfortably
            win = make_window(app, args.title, 560, 420, compact=True)
        else:
            win = make_window(app, args.title, 960, 640)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        if compact:
            splash = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            splash.add_css_class("fu-splash")
            splash.set_halign(Gtk.Align.CENTER)
            splash.set_hexpand(True)

            icon = app_icon_image(128)
            icon.add_css_class("fu-splash-icon")
            icon.set_halign(Gtk.Align.CENTER)
            splash.append(icon)

            t = Gtk.Label(label=args.title or "UrStack")
            t.add_css_class("fu-splash-title")
            t.set_halign(Gtk.Align.CENTER)
            splash.append(t)

            status_lbl = Gtk.Label(label="Checking for updates…", wrap=True)
            status_lbl.add_css_class("fu-splash-status")
            status_lbl.set_halign(Gtk.Align.CENTER)
            status_lbl.set_justify(Gtk.Justification.CENTER)
            splash.append(status_lbl)
            outer.append(splash)

            bar_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bar_wrap.set_margin_start(28)
            bar_wrap.set_margin_end(28)
            bar_wrap.set_margin_bottom(20)
        else:
            hero_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
            hero_box.add_css_class("fu-hero")
            hero_box.append(app_icon_image(48))
            titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            titles.set_hexpand(True)
            t = Gtk.Label(label=args.title, xalign=0.0)
            t.add_css_class("fu-hero-title")
            titles.append(t)
            status_lbl = Gtk.Label(label="Starting…", xalign=0.0, wrap=True)
            status_lbl.add_css_class("fu-hero-sub")
            titles.append(status_lbl)
            hero_box.append(titles)
            outer.append(hero_box)

            bar_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            bar_wrap.set_margin_start(20)
            bar_wrap.set_margin_end(20)
            bar_wrap.set_margin_bottom(4)

        progress = Gtk.ProgressBar()
        progress.set_show_text(not args.pulsate)
        progress.set_fraction(0.0)
        if not args.pulsate:
            progress.set_text("0%")
        pulse_id = {"id": 0}
        if args.pulsate:
            progress.pulse()

            def _pulse() -> bool:
                if state.get("_done"):
                    return False
                progress.pulse()
                return True

            pulse_id["id"] = GLib.timeout_add(120, _pulse)
        bar_wrap.append(progress)
        outer.append(bar_wrap)

        # Log panel — only for detailed (non-compact) progress; hidden until first line
        log_revealer = Gtk.Revealer()
        log_revealer.set_reveal_child(False)
        log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(140)
        scrolled.set_max_content_height(220)
        scrolled.set_propagate_natural_height(True)
        scrolled.set_margin_top(4)
        scrolled.set_margin_start(16)
        scrolled.set_margin_end(16)
        scrolled.set_margin_bottom(4)
        clamp = wide_clamp()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.add_css_class("card")
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        log_box.set_margin_top(10)
        log_box.set_margin_bottom(10)
        log_box.set_margin_start(12)
        log_box.set_margin_end(12)
        log_lbl = Gtk.Label(label="", xalign=0.0, wrap=True, selectable=True)
        log_lbl.add_css_class("fu-mono")
        log_lbl.set_valign(Gtk.Align.START)
        log_box.append(log_lbl)
        card.append(log_box)
        clamp.set_child(card)
        scrolled.set_child(clamp)
        log_revealer.set_child(scrolled)
        if not compact:
            outer.append(log_revealer)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.add_css_class("fu-actions")
        if compact:
            actions.set_margin_top(0)
        cancelled = {"v": False}
        lines: list[str] = []

        def finish(code: int) -> None:
            state["_done"] = True
            state["code"] = code
            win.close()

        def on_cancel(*_a: object) -> None:
            cancelled["v"] = True
            if args.cancel_flag:
                Path(args.cancel_flag).touch()
            finish(1)

        if not getattr(args, "no_cancel", False):
            cancel_btn = mk_btn("Cancel", "flat fu-secondary", "process-stop-symbolic")
            cancel_btn.set_hexpand(True)
            cancel_btn.connect("clicked", on_cancel)
            actions.append(cancel_btn)
            outer.append(pin_page_footer(actions))

        wrap_shell(win, args.title, outer, "Checking…" if args.pulsate else "Working…")

        def append_line(text: str) -> None:
            if compact:
                return
            lines.append(text)
            while len(lines) > 200:
                lines.pop(0)
            log_lbl.set_text("\n".join(lines))
            if not log_revealer.get_reveal_child():
                log_revealer.set_reveal_child(True)
                # Grow window a bit once the log appears
                win.set_default_size(720, 420)
                win.set_resizable(True)
            adj = scrolled.get_vadjustment()
            GLib.idle_add(lambda: adj.set_value(adj.get_upper()) or False)

        def handle_line(raw: str) -> None:
            line = raw.rstrip("\n")
            if not line:
                return
            if line.startswith("#"):
                msg = line[1:].strip()
                status_lbl.set_text(msg)
                append_line(msg)
            elif line.isdigit():
                pct = max(0, min(100, int(line)))
                if not args.pulsate:
                    progress.set_fraction(pct / 100.0)
                    progress.set_text(f"{pct}%")
                if pct >= 100 and args.auto_close:
                    GLib.timeout_add(400, lambda: finish(0) or False)

        def on_stdin(_source: GLib.IOChannel, condition: GLib.IOCondition) -> bool:
            if cancelled["v"]:
                return False
            if condition & GLib.IOCondition.HUP:
                if args.auto_close:
                    GLib.timeout_add(350, lambda: finish(0 if not cancelled["v"] else 1) or False)
                return False
            status, line, _hlen = _source.read_line()
            if status == GLib.IOStatus.NORMAL and line is not None:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                handle_line(line)
                return True
            if status == GLib.IOStatus.EOF:
                if args.auto_close:
                    GLib.timeout_add(350, lambda: finish(0 if not cancelled["v"] else 1) or False)
                return False
            return True

        channel = GLib.IOChannel.unix_new(sys.stdin.fileno())
        channel.set_encoding(None)
        channel.set_buffered(True)
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IOCondition.IN | GLib.IOCondition.HUP, on_stdin)

        def on_close(*_a: object) -> bool:
            state["_done"] = True
            if not cancelled["v"] and state.get("code") is None:
                state["code"] = 0
            app.quit()
            return False

        state["code"] = None  # type: ignore[assignment]
        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.progress", build, unique=False)


def mode_runs(args: argparse.Namespace) -> int:
    """Browse ~/.local/state/urstack/runs/ and open a run summary/log."""

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title, 860, 680)
        runs_dir = Path(args.runs_dir).expanduser()
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        intro = Gtk.Label(label="Detailed per-run logs from apply sessions", xalign=0.0)
        intro.add_css_class("fu-body")
        intro.add_css_class("dim-label")
        outer.append(intro)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_margin_top(8)
        scrolled.set_margin_start(16)
        scrolled.set_margin_end(16)

        clamp = wide_clamp()
        listbox = Gtk.ListBox()
        listbox.add_css_class("boxed-list")
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)

        runs: list[Path] = []
        if runs_dir.is_dir():
            runs = sorted([p for p in runs_dir.iterdir() if p.is_dir()], reverse=True)[:40]

        detail = Gtk.Label(label="Select a run to preview its summary.", xalign=0.0, wrap=True, selectable=True)
        detail.add_css_class("fu-mono")
        detail.set_margin_top(10)
        detail.set_margin_start(16)
        detail.set_margin_end(16)

        if not runs:
            row = Adw.ActionRow(title="No run logs yet")
            row.set_subtitle("Logs appear after you apply updates")
            listbox.append(row)
        else:
            for run in runs:
                meta = run / "meta.txt"
                subtitle = ""
                if meta.is_file():
                    subtitle = meta.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
                    subtitle = subtitle[0] if subtitle else ""
                row = Adw.ActionRow(title=run.name, subtitle=subtitle or str(run))
                row.set_activatable(True)
                listbox.append(row)

            def on_row(_lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
                idx = row.get_index()
                if idx < 0 or idx >= len(runs):
                    return
                run = runs[idx]
                chunks: list[str] = [f"Run: {run}\n"]
                for name in ("summary.txt", "meta.txt", "dnf-history.txt", "priv.log", "flatpak.log"):
                    path = run / name
                    if path.is_file():
                        chunks.append(f"── {name} ──\n")
                        chunks.append(path.read_text(encoding="utf-8", errors="replace")[:8000])
                        chunks.append("\n")
                # include any other *.log
                for path in sorted(run.glob("*.log")):
                    if path.name in {"priv.log", "flatpak.log"}:
                        continue
                    chunks.append(f"── {path.name} ──\n")
                    chunks.append(path.read_text(encoding="utf-8", errors="replace")[:4000])
                    chunks.append("\n")
                detail.set_text("".join(chunks) or "(empty run folder)")

            listbox.connect("row-activated", on_row)

        clamp.set_child(listbox)
        scrolled.set_child(clamp)
        outer.append(scrolled)

        detail_scroll = Gtk.ScrolledWindow()
        detail_scroll.set_min_content_height(180)
        detail_scroll.set_margin_bottom(4)
        detail_card = Gtk.Box()
        detail_card.add_css_class("card")
        detail_card.set_margin_start(16)
        detail_card.set_margin_end(16)
        inner = Gtk.Box()
        inner.set_margin_top(10)
        inner.set_margin_bottom(10)
        inner.set_margin_start(12)
        inner.set_margin_end(12)
        inner.append(detail)
        detail_card.append(inner)
        detail_scroll.set_child(detail_card)
        outer.append(detail_scroll)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        actions.add_css_class("fu-actions")
        back = mk_btn("Back", "suggested-action pill fu-primary")
        back.set_hexpand(True)
        back.connect("clicked", lambda *_: win.close())
        actions.append(back)
        outer.append(pin_page_footer(actions))
        wrap_shell(win, args.title, outer, "Run logs")

        def on_close(*_a: object) -> bool:
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.runs", build)


SETTING_KEYS: list[tuple[str, str, str, str]] = [
    # key, group, title, subtitle
    (
        "autostart",
        "Startup",
        "Launch at login",
        "Open UrStack when you log in to the desktop.",
    ),
    (
        "autostart_background",
        "Startup",
        "Check in the background at login",
        "When Launch at login is on, run a silent check instead of opening "
        "the window. The grey tray icon shows while it scans; click it if "
        "updates are waiting.",
    ),
    (
        "scan_on_startup",
        "Startup",
        "Scan when UrStack opens",
        "Check for software and health updates as soon as the app starts. Turn this off to open the window first and scan with Refresh.",
    ),
    (
        "daily_check",
        "Startup",
        "Daily silent check",
        "Once a day, check for updates in the background and notify if anything is waiting. Does not apply updates.",
    ),
    (
        "notifications",
        "Startup",
        "Desktop notifications",
        "Show a notification when updates are found, and when an apply finishes.",
    ),
    (
        "enable_dnf",
        "Core updates",
        "DNF packages",
        "Check and apply RPM updates from Fedora and your enabled repositories (dnf/dnf5).",
    ),
    (
        "enable_flatpak",
        "Core updates",
        "Flatpak",
        "Update Flatpak apps and runtimes (system and user installs, including Flathub).",
    ),
    (
        "enable_snap",
        "Core updates",
        "Snap",
        "Refresh Snap packages when snapd is installed.",
    ),
    (
        "enable_fw",
        "Core updates",
        "Check firmware",
        "Look for device firmware with fwupd (UEFI, docks, peripherals). Showing updates does not install them.",
    ),
    (
        "apply_fw",
        "Core updates",
        "Apply firmware updates",
        "Install fwupd payloads when you Apply (and in urstack --yes). Off by default because a flash may need a reboot. You can still tick Firmware on the Apply screen for a one-off.",
    ),
    (
        "enable_kernel_prune",
        "Core updates",
        "Prune old kernels",
        "After DNF updates, remove older kernels while keeping a few for recovery if a new one fails.",
    ),
    (
        "exclude_discover",
        "Core updates",
        "Exclude Plasma Discover",
        "Tell DNF not to pull Plasma Discover back in if you removed it (KDE systems).",
    ),
    (
        "quiet_gnome_software",
        "Core updates",
        "Quiet GNOME Software",
        "Temporarily pause GNOME Software’s background service while UrStack runs, to avoid clashes.",
    ),
    (
        "enable_toolbox",
        "Containers",
        "Toolbx / Distrobox",
        "Update packages inside Toolbx or Distrobox containers when those tools are present.",
    ),
    (
        "enable_npm",
        "Developer tools",
        "npm global (nvm)",
        "Check globally installed npm packages for your active nvm Node version.",
    ),
    (
        "enable_npm_user",
        "Developer tools",
        "npm user (~/.local)",
        "Check npm packages installed to your user prefix under ~/.local.",
    ),
    (
        "enable_pip",
        "Developer tools",
        "pip (user)",
        "Check Python packages installed with pip --user.",
    ),
    (
        "enable_pipx",
        "Developer tools",
        "pipx",
        "Check apps installed with pipx (isolated Python CLIs).",
    ),
    (
        "enable_rust",
        "Developer tools",
        "rustup",
        "Check for Rust toolchain updates via rustup.",
    ),
    (
        "enable_cargo",
        "Developer tools",
        "Cargo binaries",
        "Check crates installed with cargo install (needs cargo-update for full checks).",
    ),
    (
        "enable_node",
        "Developer tools",
        "Node.js (nvm)",
        "Compare your nvm default Node version with the latest available release.",
    ),
    (
        "enable_cursor",
        "Apps & CLIs",
        "Cursor",
        "Check whether the Cursor editor has a newer build available.",
    ),
    (
        "enable_claude",
        "Apps & CLIs",
        "Claude Code",
        "Check for updates to the Claude Code CLI.",
    ),
    (
        "enable_supabase",
        "Apps & CLIs",
        "Supabase CLI",
        "Check for updates to the Supabase command-line tool.",
    ),
    (
        "enable_jetbrains",
        "Advisories",
        "JetBrains Toolbox",
        "Show a reminder only — UrStack does not auto-update JetBrains IDEs.",
    ),
    (
        "enable_appimage",
        "Advisories",
        "AppImages",
        "List AppImages under ~/Applications as an advisory; updates are still manual.",
    ),
]

# Match lib/core/common.sh cfg_get defaults so Settings doesn't show (and save) the wrong state.
SETTING_DEFAULTS: dict[str, str] = {
    "autostart": "0",
    "autostart_background": "0",
    "scan_on_startup": "1",
    "daily_check": "0",
    "notifications": "1",
    "enable_dnf": "1",
    "enable_flatpak": "1",
    "enable_snap": "1",
    "enable_fw": "1",
    "apply_fw": "0",
    "enable_kernel_prune": "1",
    "exclude_discover": "1",
    "quiet_gnome_software": "1",
}


def setting_default(key: str) -> str:
    return SETTING_DEFAULTS.get(key, "0")


def read_config_map(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or "=" not in line:
            continue
        key, val = line.split("=", 1)
        values[key.strip()] = val.strip()
    return values


def xdg_autostart_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "urstack.desktop"


def urstack_launch_command() -> str:
    from shutil import which

    found = which("urstack")
    if found:
        return found
    bundled = APP_ROOT / "bin" / "urstack"
    if bundled.is_file():
        return str(bundled)
    return "urstack"


def autostart_desktop_text(
    exec_cmd: str | None = None, *, background: bool = False
) -> str:
    cmd = exec_cmd or urstack_launch_command()
    if background:
        cmd = f"{cmd} --check --tray"
    comment = (
        "Check for Fedora stack updates at login"
        if background
        else "Open UrStack at login"
    )
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=UrStack\n"
        f"Comment={comment}\n"
        f"Exec={cmd}\n"
        "Icon=urstack\n"
        "Terminal=false\n"
        "Categories=System;Settings;PackageManager;\n"
        "StartupNotify=false\n"
        "StartupWMClass=urstack\n"
        "X-GNOME-Autostart-enabled=true\n"
        "X-GNOME-Autostart-Delay=10\n"
    )


def sync_xdg_autostart(
    enabled: bool, *, background: bool = False, path: Path | None = None
) -> Path:
    """Install or remove ~/.config/autostart/urstack.desktop."""
    dest = path or xdg_autostart_path()
    if enabled:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(autostart_desktop_text(background=background), encoding="utf-8")
        dest.chmod(0o644)
    else:
        dest.unlink(missing_ok=True)
    return dest


def systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def daily_check_unit_texts(exec_cmd: str | None = None) -> tuple[str, str]:
    cmd = exec_cmd or urstack_launch_command()
    service = (
        "[Unit]\n"
        "Description=UrStack — check for workstation updates\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={cmd} --check\n"
        "SuccessExitStatus=0 1\n"
    )
    timer = (
        "[Unit]\n"
        "Description=Daily UrStack update check\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=daily\n"
        "Persistent=true\n"
        "RandomizedDelaySec=30m\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return service, timer


def _apply_user_systemctl() -> bool:
    return os.environ.get("URSTACK_APPLY_SYSTEMD", "1") != "0"


def _user_systemctl(*args: str) -> None:
    if not _apply_user_systemctl():
        return
    try:
        subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def sync_daily_check_timer(
    enabled: bool, *, unit_dir: Path | None = None
) -> Path:
    """Install or remove the user systemd daily check timer."""
    dest = unit_dir or systemd_user_dir()
    service = dest / "urstack-check.service"
    timer = dest / "urstack-check.timer"
    if enabled:
        dest.mkdir(parents=True, exist_ok=True)
        svc_text, tmr_text = daily_check_unit_texts()
        service.write_text(svc_text, encoding="utf-8")
        timer.write_text(tmr_text, encoding="utf-8")
        _user_systemctl("disable", "--now", "stackup-check.timer")
        _user_systemctl("disable", "--now", "fedora-updates-check.timer")
        _user_systemctl("daemon-reload")
        _user_systemctl("enable", "--now", "urstack-check.timer")
    else:
        _user_systemctl("disable", "--now", "urstack-check.timer")
        service.unlink(missing_ok=True)
        timer.unlink(missing_ok=True)
        _user_systemctl("daemon-reload")
    return dest


def write_config_map(path: Path, values: dict[str, str]) -> None:
    from datetime import datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    previous = read_config_map(path) if path.is_file() else {}
    if path.is_file():
        bak = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
        bak.write_bytes(path.read_bytes())

    known = {key for key, _group, _title, _sub in SETTING_KEYS} | {
        "keep_kernels",
        "appearance",
    }
    # Preserve backup_* and any other freeform keys Settings doesn't edit
    merged = dict(previous)
    merged.update(values)

    lines = [
        "# UrStack — saved from Settings",
        f"# {datetime.now().isoformat(timespec='seconds')}",
        "# Re-scan: urstack --detect --write-config",
        "",
        "# ── Startup ───────────────────────────────────────────────────────────────────",
    ]
    for key, group, _title, _sub in SETTING_KEYS:
        if group == "Startup":
            lines.append(f"{key}={merged.get(key, setting_default(key))}")
    lines.append("")
    lines.append("# ── Core / plugins ──────────────────────────────────────────────────────────")
    for key, group, _title, _sub in SETTING_KEYS:
        if group != "Startup":
            lines.append(f"{key}={merged.get(key, setting_default(key))}")
    lines.append("")
    lines.append("# ── Behaviour ────────────────────────────────────────────────────────────────")
    lines.append(f"keep_kernels={merged.get('keep_kernels', '3')}")
    lines.append(f"appearance={normalize_appearance(merged.get('appearance'))}")
    extras = sorted(k for k in merged if k not in known)
    if extras:
        lines.append("")
        lines.append("# ── Backup / other ───────────────────────────────────────────────────────────")
        for key in extras:
            lines.append(f"{key}={merged[key]}")
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        sync_xdg_autostart(
            merged.get("autostart", setting_default("autostart")) == "1",
            background=merged.get(
                "autostart_background", setting_default("autostart_background")
            )
            == "1",
        )
    except OSError:
        pass
    try:
        sync_daily_check_timer(
            merged.get("daily_check", setting_default("daily_check")) == "1"
        )
    except OSError:
        pass


def _look_py_command() -> list[str]:
    return [sys.executable or "python3", str(Path(__file__).resolve().parent / "look.py")]


def _look_file_filters() -> tuple[Gio.ListStore, Gtk.FileFilter]:
    store = Gio.ListStore.new(Gtk.FileFilter)
    filt = Gtk.FileFilter()
    filt.set_name("Theme archives")
    for pat in (
        "*.tar",
        "*.tar.xz",
        "*.tar.gz",
        "*.tar.bz2",
        "*.tgz",
        "*.txz",
        "*.zip",
    ):
        filt.add_pattern(pat)
    store.append(filt)
    allf = Gtk.FileFilter()
    allf.set_name("All files")
    allf.add_pattern("*")
    store.append(allf)
    return store, filt


def _clear_box(box: Gtk.Widget) -> None:
    child = box.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        box.remove(child)
        child = nxt


def _look_preview_async(pic: Gtk.Picture, url: str) -> None:
    gen = int(getattr(pic, "_prev_gen", 0)) + 1
    pic._prev_gen = gen  # type: ignore[attr-defined]

    def work() -> None:
        try:
            path = theme_store_mod.cached_preview(url)
        except Exception:  # noqa: BLE001
            path = None

        def apply() -> bool:
            if int(getattr(pic, "_prev_gen", 0)) != gen:
                return False
            if path is None or not Path(path).is_file():
                return False
            try:
                pic.set_file(Gio.File.new_for_path(str(path)))
            except Exception:  # noqa: BLE001
                return False
            return False

        GLib.idle_add(apply)

    threading.Thread(target=work, daemon=True).start()


def _theme_detail_byline(info: dict) -> str:
    bits: list[str] = []
    author = str(info.get("author") or "").strip()
    if author:
        bits.append(author)
    license_ = str(info.get("license") or "").strip()
    if license_ and license_.lower() not in {"unknown", "none"}:
        bits.append(license_)
    version = str(info.get("version") or "").strip()
    if version:
        bits.append(version)
    source = str(info.get("source") or "").strip()
    if source:
        bits.append(source)
    downloads = theme_store_mod.format_count(str(info.get("downloads") or ""))
    if downloads and downloads not in {"0", ""}:
        bits.append(f"{downloads} downloads")
    return " · ".join(bits)


def build_theme_detail_content(
    row: dict[str, str],
    *,
    on_install: Callable[[], None] | None = None,
    on_open_url: Callable[[str], None] | None = None,
    parent_win: Gtk.Window | None = None,
) -> Gtk.Widget:
    """Full-page / dialog body for one Look catalog theme — same shape as Apps."""
    info = theme_store_mod.details_from_row(row)
    name = (info.get("name") or row.get("name") or "Theme").strip() or "Theme"

    main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    main.set_hexpand(True)
    main.set_vexpand(True)
    main.set_margin_start(PAGE_SIDE_PAD)
    main.set_margin_end(PAGE_SIDE_PAD)

    hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    hero.add_css_class("fu-page-hero")
    hero.set_hexpand(True)

    pic = Gtk.Picture()
    pic.set_size_request(120, 72)
    pic.set_valign(Gtk.Align.START)
    try:
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
    except Exception:  # noqa: BLE001
        pass
    preview = str(info.get("preview") or "").strip()
    if preview:
        _look_preview_async(pic, preview)
        hero.append(pic)
    else:
        icon = Gtk.Image.new_from_icon_name(page_icon("look"))
        icon.set_pixel_size(48)
        icon.set_valign(Gtk.Align.START)
        hero.append(icon)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    texts.set_hexpand(True)
    title = Gtk.Label(label=name, xalign=0.0, wrap=True)
    title.add_css_class("fu-hero-title")
    texts.append(title)
    summary = str(info.get("summary") or "").strip()
    sub = Gtk.Label(label=summary, xalign=0.0, wrap=True)
    sub.add_css_class("fu-hero-sub")
    sub.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    sub.set_visible(bool(summary))
    texts.append(sub)
    byline = Gtk.Label(label=_theme_detail_byline(info), xalign=0.0, wrap=True)
    byline.add_css_class("fu-app-byline")
    byline.set_visible(bool(byline.get_text()))
    texts.append(byline)
    hero.append(texts)

    status = Gtk.Label(label=str(info.get("source") or "Theme"))
    status.add_css_class("fu-badge")
    status.set_valign(Gtk.Align.START)
    hero.append(status)
    main.append(hero)

    scrolled, _clamp, box = page_scroll_body(spacing=14)
    box.set_margin_bottom(8)

    shots_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    shots_host.set_visible(False)
    shot_strip: list[Gtk.Box] = []
    box.append(shots_host)

    about_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    about_host.set_visible(False)
    about_lab = Gtk.Label(label="", xalign=0.0, wrap=True, selectable=True)
    about_lab.add_css_class("fu-app-desc")
    about_lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    about_lab.set_margin_start(16)
    about_lab.set_margin_end(16)
    about_host.append(page_section_label("About"))
    about_host.append(about_lab)
    box.append(about_host)

    facts_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.append(facts_host)

    def link_handler(uri: str) -> None:
        if on_open_url is not None:
            on_open_url(uri)
        else:
            _open_uri(uri, parent_win)

    def add_link_row(group: Adw.PreferencesGroup, title: str, uri: str) -> None:
        if not uri:
            return
        fact = _detail_fact_row(title, uri)
        try:
            fact.set_activatable(True)
        except Exception:  # noqa: BLE001
            pass
        fact.connect("activated", lambda *_a, u=uri: link_handler(u))
        open_btn = Gtk.Button.new_from_icon_name(
            pick_icon("adw-external-link-symbolic", "web-browser-symbolic")
        )
        open_btn.add_css_class("flat")
        open_btn.set_valign(Gtk.Align.CENTER)
        open_btn.set_tooltip_text("Open link")
        open_btn.connect("clicked", lambda *_a, u=uri: link_handler(u))
        fact.add_suffix(open_btn)
        group.add(fact)

    def fill_facts(current: dict) -> None:
        while facts_host.get_first_child() is not None:
            facts_host.remove(facts_host.get_first_child())
        group = Adw.PreferencesGroup(title="Details")
        group.add(_detail_fact_row("Source", str(current.get("source") or "—")))
        kind = str(current.get("typename") or current.get("kind") or "").strip()
        if kind:
            group.add(_detail_fact_row("Type", kind.replace("-", " ").title()))
        author = str(current.get("author") or "").strip()
        if author:
            group.add(_detail_fact_row("Author", author))
        license_ = str(current.get("license") or "").strip()
        if license_:
            group.add(_detail_fact_row("License", license_))
        version = str(current.get("version") or "").strip()
        if version:
            group.add(_detail_fact_row("Version", version))
        downloads = theme_store_mod.format_count(str(current.get("downloads") or ""))
        if downloads and downloads not in {"0", ""}:
            group.add(_detail_fact_row("Downloads", downloads))
        github = str(current.get("github") or "").strip()
        if github:
            group.add(_detail_fact_row("Repository", github))
        homepage = str(current.get("homepage") or current.get("detailpage") or "").strip()
        add_link_row(
            group,
            "GitHub" if current.get("host") == "catalog" else "Listing",
            homepage,
        )
        facts_host.append(group)

    loaded_shots: set[str] = set()

    def fill_shots(current: dict) -> None:
        shots = current.get("screenshots") or []
        if not isinstance(shots, list) or not shots:
            return
        try:
            import app_meta as _app_meta
        except ImportError:
            return
        if not shot_strip:
            shots_host.append(page_section_label("Screenshots"))
            shot_scroll = Gtk.ScrolledWindow()
            shot_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            shot_scroll.set_hexpand(True)
            strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            strip.add_css_class("fu-shot-strip")
            strip.set_margin_start(12)
            strip.set_margin_end(12)
            shot_scroll.set_child(strip)
            shots_host.append(shot_scroll)
            shot_strip.append(strip)
        strip = shot_strip[0]
        shots_host.set_visible(True)

        gallery: list[dict[str, str]] = []
        for shot in shots[:8]:
            if not isinstance(shot, dict):
                continue
            thumb = str(shot.get("thumb") or shot.get("full") or "").strip()
            full = str(shot.get("full") or thumb).strip()
            if thumb:
                gallery.append({"thumb": thumb, "full": full})
        if not gallery:
            return

        def on_shot(path: Path | None, full: str, idx: int) -> bool:
            if path is None or str(path) in loaded_shots:
                return False
            loaded_shots.add(str(path))
            _append_detail_shot(
                strip, path, full, parent_win, gallery=gallery, index=idx
            )
            return False

        for idx, item in enumerate(gallery):
            _app_meta.fetch_shot_async(
                item["thumb"],
                lambda p, u=item["full"], i=idx: GLib.idle_add(on_shot, p, u, i),
            )

    def apply_meta(current: dict | None) -> bool:
        if not isinstance(current, dict):
            return False
        info.update(current)
        next_summary = str(info.get("summary") or "").strip()
        sub.set_label(next_summary)
        sub.set_visible(bool(next_summary))
        byline.set_label(_theme_detail_byline(info))
        byline.set_visible(bool(byline.get_text()))
        status.set_label(str(info.get("source") or "Theme"))
        desc = str(info.get("description") or "").strip()
        if desc:
            about_lab.set_label(desc)
            about_host.set_visible(True)
        fill_facts(info)
        fill_shots(info)
        return False

    fill_facts(info)
    desc0 = str(info.get("description") or "").strip()
    if desc0:
        about_lab.set_label(desc0)
        about_host.set_visible(True)
    fill_shots(info)
    if (row.get("host") or "") in theme_store_mod.HOSTS:
        theme_store_mod.fetch_details_async(
            row, lambda m: GLib.idle_add(apply_meta, m)
        )

    main.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    actions.add_css_class("fu-actions")
    homepage = str(info.get("homepage") or info.get("detailpage") or "").strip()
    if on_install is not None:
        install_btn = mk_btn(
            f"Install {name}",
            "suggested-action pill fu-primary",
            pick_icon("software-install-symbolic", "emblem-ok-symbolic"),
        )
        install_btn.set_hexpand(True)
        install_btn.connect("clicked", lambda *_: on_install())
        actions.append(install_btn)
    if homepage and on_open_url is not None:
        visit = mk_btn(
            "Open listing" if (row.get("host") or "") != "catalog" else "Open on GitHub",
            "pill fu-secondary",
            pick_icon("adw-external-link-symbolic", "web-browser-symbolic"),
        )
        visit.set_hexpand(True)
        visit.connect("clicked", lambda *_: on_open_url(homepage))
        actions.append(visit)
    if actions.get_first_child() is not None:
        main.append(pin_page_footer(actions))
    return main


def _show_theme_details(
    source: Gtk.Widget,
    row: dict[str, str],
    on_store_install: Callable[[str, str], None],
) -> None:
    """Push a theme details page in the shell, matching Apps."""
    parent = _widget_window(source)
    name = (row.get("name") or "Theme").strip() or "Theme"
    host = (row.get("host") or "").strip() or "theme"
    tid = (row.get("id") or name).strip() or "theme"
    closer: dict[str, Callable[[], None] | None] = {"fn": None}
    closed = {"v": False}

    def close_detail() -> None:
        if closed["v"]:
            return
        closed["v"] = True
        fn = closer.get("fn")
        if fn is not None:
            fn()

    def do_install() -> None:
        close_detail()
        on_store_install(host, tid)

    def do_open_url(uri: str = "") -> None:
        _open_uri(
            uri or row.get("detailpage") or row.get("homepage") or "",
            parent,
        )

    content = build_theme_detail_content(
        row,
        on_install=do_install,
        on_open_url=do_open_url,
        parent_win=parent,
    )

    nav = _find_navigation_view(source)
    if nav is not None:
        tag = f"theme-{host}-{tid}"
        try:
            existing = nav.find_page(tag)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            try:
                nav.pop_to_page(existing)
                return
            except Exception:  # noqa: BLE001
                pass
        page = make_nav_page(name, content, tag=tag)
        nav.push(page)
        closer["fn"] = lambda n=nav: bool(n.pop())
        return

    try:
        dialog = Adw.Dialog()
        try:
            dialog.set_title(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            dialog.set_content_width(560)
            dialog.set_content_height(640)
        except Exception:  # noqa: BLE001
            pass
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        dialog.set_child(toolbar)
        closer["fn"] = dialog.close
        dialog.present(parent)
    except Exception:  # noqa: BLE001
        pass


def _look_store_section(
    *,
    parent_win: Gtk.Window,
    desktop: str,
    on_store_install: Callable[[str, str], None],
) -> Gtk.Widget:
    """Browse community themes: GitHub picks plus GNOME Look / KDE Look."""
    kinds = theme_store_mod.categories_for(desktop)
    if not kinds:
        kinds = ["all", "looks", "gtk", "icons", "cursors"]
    state = {"kind": theme_store_mod.default_kind(desktop), "q": "", "gen": 0}
    if state["kind"] not in kinds:
        state["kind"] = kinds[0]

    kind_icons = {
        "all": pick_icon("view-grid-symbolic", "view-app-grid-symbolic", page_icon("look")),
        "looks": page_icon("look"),
        "gtk": pick_icon(
            "urstack-look-symbolic",
            "color-select-symbolic",
            "applications-graphics-symbolic",
        ),
        "plasma": pick_icon(
            "preferences-desktop-wallpaper-symbolic",
            "urstack-look-symbolic",
            page_icon("look"),
        ),
        "icons": pick_icon("folder-pictures-symbolic", "emblem-photos-symbolic"),
        "cursors": pick_icon("input-mouse-symbolic", "input-tablet-symbolic"),
    }

    wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    wrap.append(
        page_callout(
            "Community catalog",
            "Click a pack for screenshots and details, like Apps. "
            "GitHub picks (Dracula, Nord, Catppuccin, Sweet, Bibata) sit at the top. "
            "The rest is the GNOME Look / KDE Look catalog — the same open-source "
            "store as Discover. Install unpacks a free archive into your home "
            "directory and switches this desktop to it.",
        )
    )

    cat_rail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    cat_rail.add_css_class("fu-cat-rail")
    cat_scroll = Gtk.ScrolledWindow()
    cat_scroll.add_css_class("fu-cat-scroll")
    cat_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    cat_scroll.set_hexpand(True)
    cat_scroll.set_vexpand(False)
    try:
        cat_scroll.set_propagate_natural_height(True)
        cat_scroll.set_overlay_scrolling(True)
    except Exception:  # noqa: BLE001
        pass
    cat_scroll.set_child(cat_rail)
    wrap.append(cat_scroll)

    search = Gtk.SearchEntry()
    search.set_placeholder_text("Search themes…")
    search.add_css_class("fu-apps-search")
    search.set_hexpand(True)
    wrap.append(search)

    status = Gtk.Label(label="Loading…", xalign=0.0)
    status.add_css_class("fu-app-mini-sub")
    wrap.append(status)

    host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    host.set_hexpand(True)
    wrap.append(host)

    cat_btns: dict[str, Gtk.ToggleButton] = {}
    cat_guard = {"busy": False}
    search_timeout = {"id": 0}

    def make_card(row: dict[str, str]) -> Gtk.Widget:
        row = dict(row)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("fu-look-card")
        card.set_hexpand(True)
        card.set_halign(Gtk.Align.FILL)
        try:
            card.set_overflow(Gtk.Overflow.HIDDEN)
        except Exception:  # noqa: BLE001
            pass

        pic = Gtk.Picture()
        pic.add_css_class("fu-look-card-preview")
        pic.set_hexpand(True)
        pic.set_vexpand(True)
        try:
            pic.set_can_shrink(True)
        except Exception:  # noqa: BLE001
            pass
        try:
            # GitHub OpenGraph cards are 2:1; COVER in a short strip cropped them
            # to a middle slice. CONTAIN keeps the whole screenshot visible.
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        except Exception:  # noqa: BLE001
            try:
                pic.set_keep_aspect_ratio(True)
            except Exception:  # noqa: BLE001
                pass
        frame = Gtk.AspectFrame(obey_child=False, ratio=16 / 9, xalign=0.5, yalign=0.5)
        frame.add_css_class("fu-look-card-preview-frame")
        frame.set_hexpand(True)
        frame.set_halign(Gtk.Align.FILL)
        frame.set_child(pic)
        preview = (row.get("preview") or "").strip()
        if not preview and row.get("github"):
            preview = theme_store_mod.github_opengraph_url(row["github"])
        card.append(frame)
        if preview:
            _look_preview_async(pic, preview)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        body.add_css_class("fu-look-card-body")
        name = Gtk.Label(label=row.get("name") or "", xalign=0.0)
        name.add_css_class("fu-app-mini-title")
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_single_line_mode(True)
        body.append(name)
        bits: list[str] = []
        if row.get("author"):
            bits.append(row["author"])
        if row.get("host") == "catalog":
            if row.get("license"):
                bits.append(row["license"])
            bits.append(theme_store_mod.source_label(row))
        else:
            downloads = theme_store_mod.format_count(row.get("downloads") or "")
            if downloads:
                bits.append(f"{downloads} downloads")
            bits.append(theme_store_mod.source_label(row))
        sub = Gtk.Label(label=" · ".join(bits), xalign=0.0)
        sub.add_css_class("fu-app-mini-sub")
        sub.set_ellipsize(Pango.EllipsizeMode.END)
        body.append(sub)
        if row.get("summary"):
            blurb = Gtk.Label(label=row["summary"], xalign=0.0)
            blurb.add_css_class("fu-app-mini-sub")
            blurb.set_ellipsize(Pango.EllipsizeMode.END)
            blurb.set_lines(2)
            blurb.set_wrap(True)
            body.append(blurb)

        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        inst = Gtk.Button(label="Install")
        inst.add_css_class("pill")
        inst.add_css_class("suggested-action")
        inst.set_hexpand(True)
        host_id = row.get("host") or ""
        content_id = row.get("id") or ""
        inst.connect(
            "clicked",
            lambda *_a, h=host_id, i=content_id: on_store_install(h, i),
        )
        foot.append(inst)
        detail = (row.get("detailpage") or "").strip()
        if detail:
            openb = Gtk.Button.new_from_icon_name(
                pick_icon("adw-external-link-symbolic", "web-browser-symbolic")
            )
            openb.add_css_class("flat")
            openb.set_tooltip_text(
                "Open on GitHub" if row.get("host") == "catalog" else "Open listing"
            )
            openb.connect("clicked", lambda *_a, u=detail: _open_uri(u, parent_win))
            foot.append(openb)
        body.append(foot)
        card.append(body)
        card.set_tooltip_text(f"Details for {row.get('name') or 'theme'}")
        try:
            card.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        except Exception:  # noqa: BLE001
            pass
        click = Gtk.GestureClick()
        click.set_button(1)

        def on_card_pressed(
            gesture: Gtk.GestureClick,
            n_press: int,
            x: float,
            y: float,
            *,
            ar: dict[str, str] = row,
        ) -> None:
            if n_press != 1:
                return
            target = card.pick(x, y, Gtk.PickFlags.DEFAULT)
            wdg = target
            while wdg is not None and wdg is not card:
                if isinstance(wdg, Gtk.Button):
                    return
                wdg = wdg.get_parent()
            try:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            except Exception:  # noqa: BLE001
                pass
            _show_theme_details(card, ar, on_store_install)

        click.connect("pressed", on_card_pressed)
        card.add_controller(click)
        return card

    def show_status_only(message: str, *, spin: bool = False) -> None:
        _clear_box(host)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        if spin:
            spinner = Gtk.Spinner()
            spinner.set_size_request(28, 28)
            spinner.start()
            box.append(spinner)
        lab = Gtk.Label(label=message, wrap=True)
        lab.set_justify(Gtk.Justification.CENTER)
        lab.add_css_class("fu-app-mini-sub")
        box.append(lab)
        host.append(box)

    def apply_rows(gen: int, rows: list[dict[str, str]], err: str, store_label: str) -> bool:
        if gen != state["gen"]:
            return False
        if err:
            status.set_label(err)
            show_status_only(err)
            return False
        kind_lab = theme_store_mod.category_label(state["kind"])
        origin = store_label or "the theme store"
        if not rows:
            status.set_label(f"No {kind_lab.lower()} on {origin}")
            show_status_only("Nothing matched. Try another search or category.")
            return False
        status.set_label(
            f"{len(rows)} {kind_lab.lower()} from {origin} · user-local install"
        )
        _clear_box(host)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_homogeneous(True)
        flow.set_max_children_per_line(5)
        flow.set_min_children_per_line(5)
        flow.set_row_spacing(10)
        flow.set_column_spacing(10)
        flow.set_hexpand(True)
        flow.set_halign(Gtk.Align.FILL)
        for row in rows:
            flow.append(make_card(row))
        host.append(flow)
        return False

    def reload() -> None:
        state["gen"] += 1
        gen = state["gen"]
        kind = state["kind"]
        query = state["q"]
        kind_lab = theme_store_mod.category_label(kind)
        status.set_label(f"Loading {kind_lab.lower()}…")
        show_status_only(f"Loading {kind_lab.lower()}…", spin=True)

        def work() -> None:
            rows: list[dict[str, str]] = []
            err = ""
            label = ""
            try:
                rows, label = theme_store_mod.list_themes(kind, desktop, search=query)
            except theme_store_mod.ThemeStoreError as exc:
                err = str(exc)
            except Exception as exc:  # noqa: BLE001
                err = f"Could not load themes ({exc})"
            GLib.idle_add(apply_rows, gen, rows, err, label)

        threading.Thread(target=work, daemon=True).start()

    def sync_cat_btns() -> None:
        cat_guard["busy"] = True
        try:
            current = state["kind"]
            for key, btn in cat_btns.items():
                btn.set_active(key == current)
        finally:
            cat_guard["busy"] = False

    def on_cat_toggle(btn: Gtk.ToggleButton, key: str) -> None:
        if cat_guard["busy"]:
            return
        if btn.get_active():
            if state["kind"] == key:
                return
            state["kind"] = key
            sync_cat_btns()
            reload()
            return
        if state["kind"] == key:
            cat_guard["busy"] = True
            try:
                btn.set_active(True)
            finally:
                cat_guard["busy"] = False

    for cid in kinds:
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        icon = Gtk.Image.new_from_icon_name(kind_icons.get(cid, page_icon("look")))
        icon.set_pixel_size(16)
        inner.append(icon)
        inner.append(Gtk.Label(label=theme_store_mod.category_label(cid)))
        btn = Gtk.ToggleButton()
        btn.set_child(inner)
        btn.add_css_class("flat")
        btn.add_css_class("fu-cat-pill")
        btn.set_active(cid == state["kind"])
        btn.connect("toggled", lambda b, k=cid: on_cat_toggle(b, k))
        cat_btns[cid] = btn
        cat_rail.append(btn)

    def on_search(*_a: object) -> None:
        if search_timeout["id"]:
            GLib.source_remove(search_timeout["id"])

        def fire() -> bool:
            search_timeout["id"] = 0
            state["q"] = (search.get_text() or "").strip()
            reload()
            return False

        search_timeout["id"] = GLib.timeout_add(380, fire)

    search.connect("search-changed", on_search)
    reload()
    return wrap


def build_look_content(
    *,
    parent_win: Gtk.Window,
    on_export: Callable[[str, str], None],
    on_install: Callable[[str], None],
    on_store_install: Callable[[str, str], None] | None = None,
) -> Gtk.Widget:
    """Current desktop look: pack it, or install a theme archive."""
    outer = page_frame()
    try:
        snap_d = look_engine.inspect_look().as_dict()
    except Exception:  # noqa: BLE001
        snap_d = {
            "desktop": "unknown",
            "desktop_label": "This desktop",
            "summary": "Could not read the current look",
            "preview": "",
            "items": [],
        }

    chrome = page_chrome_box()
    chrome.append(
        page_hero(
            (str(snap_d.get("desktop") or "desktop")).capitalize(),
            "desktop",
            snap_d.get("summary") or "Current look",
            "Pack what is on screen, install a theme archive, or download a community palette from GitHub.",
            warn=False,
            ok=True,
            heading="Look",
            heading_sub="The theme this workstation is using, plus community packs (Dracula, Nord, Catppuccin, Sweet).",
            icon_name=page_icon("look"),
        )
    )
    outer.append(chrome)

    scrolled, _clamp, col = page_scroll_body(spacing=14, side_pad=0)

    preview = str(snap_d.get("preview") or "")
    if preview and Path(preview).is_file():
        pic_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        pic_wrap.add_css_class("fu-page-card")
        cap = Gtk.Label(label="Current wallpaper", xalign=0.0)
        cap.add_css_class("fu-page-card-title")
        pic_wrap.append(cap)
        try:
            picture = Gtk.Picture.new_for_filename(preview)
            picture.set_size_request(-1, 180)
            try:
                picture.set_content_fit(Gtk.ContentFit.COVER)
            except Exception:  # noqa: BLE001
                pass
            picture.set_hexpand(True)
            pic_wrap.append(picture)
        except Exception:  # noqa: BLE001
            pic_wrap.append(
                Gtk.Label(label=Path(preview).name, xalign=0.0, wrap=True)
            )
        col.append(pic_wrap)

    col.append(
        page_callout(
            "What this is",
            "Backup still saves the whole workstation. This page is only the look: "
            "the live wallpaper, icon and cursor themes, widgets, colors, and GTK/Plasma theme. "
            "Install stays in your home directory — it will not write to /usr.",
        )
    )

    col.append(page_section_label("On this desktop"))
    group = Adw.PreferencesGroup()
    wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    wrap.add_css_class("fu-page-body")
    for item in snap_d.get("items") or []:
        row = Adw.ActionRow(
            title=str(item.get("title") or item.get("id") or ""),
            subtitle=str(item.get("value") or "—"),
        )
        try:
            row.set_subtitle_lines(2)
        except AttributeError:
            pass
        note = str(item.get("note") or "")
        badge = Gtk.Label(label="Packed" if item.get("bundled") else "Name only")
        badge.add_css_class("fu-badge")
        if item.get("bundled"):
            badge.add_css_class("fu-badge-ok")
        badge.set_valign(Gtk.Align.CENTER)
        row.add_suffix(badge)
        if note:
            row.set_subtitle(f"{item.get('value') or '—'} · {note}")
        group.add(row)
    wrap.append(group)
    col.append(wrap)

    if on_store_install is not None:
        col.append(page_section_label("Browse themes"))
        col.append(
            _look_store_section(
                parent_win=parent_win,
                desktop=str(snap_d.get("desktop") or "unknown"),
                on_store_install=on_store_install,
            )
        )

    col.append(page_section_label("Include in the pack"))
    include_group = Adw.PreferencesGroup(
        description="Turn off anything you do not want in the archive. Names of Fedora-shipped themes are always recorded."
    )
    include_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    include_wrap.add_css_class("fu-page-body")
    include_labels = {
        "wallpaper": ("Wallpaper", "The image files currently on the desktop and lock screen."),
        "icons": ("Icons", "The active icon theme, if it is custom or user-installed."),
        "cursors": ("Cursors", "The active cursor theme, if it is custom."),
        "gtk": ("Application / Plasma theme", "GTK theme and Plasma look-and-feel when they are custom."),
        "colors": ("Color scheme", "Plasma colour scheme file when it lives in your home."),
        "widgets": ("Widgets & panels", "Custom plasmoids plus the layout configs for this desktop."),
        "fonts": ("User fonts", "Font files under ~/.local/share/fonts that this look names."),
        "layout": ("Desktop settings", "kdeglobals, kwin, appletsrc, GTK settings, dconf / xfconf."),
    }
    switches: dict[str, Gtk.Switch] = {}
    present = {str(it.get("id")) for it in (snap_d.get("items") or [])}
    for key in look_engine.INCLUDE_KEYS:
        title, sub = include_labels.get(key, (key, ""))
        row = Adw.ActionRow(title=title, subtitle=sub)
        try:
            row.set_subtitle_lines(2)
        except AttributeError:
            pass
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(key in present or key == "layout")
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        include_group.add(row)
        switches[key] = sw
    include_wrap.append(include_group)
    col.append(include_wrap)

    col.append(page_section_label("Theme archive"))
    archive_state = {"path": ""}
    arch_group = Adw.PreferencesGroup()
    arch_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    arch_wrap.add_css_class("fu-page-body")
    arch_row = Adw.ActionRow(
        title="No archive selected",
        subtitle="Open a .tar.xz, .tar.gz, or .zip — UrStack look packs, icon themes, GTK themes, Plasma widgets, or wallpaper.",
    )
    try:
        arch_row.set_subtitle_lines(3)
    except AttributeError:
        pass
    open_btn = mk_btn("Open…", "fu-row-suffix", "document-open-symbolic")
    open_btn.set_valign(Gtk.Align.CENTER)
    arch_row.add_suffix(open_btn)
    arch_group.add(arch_row)
    arch_wrap.append(arch_group)
    col.append(arch_wrap)

    install_btn_ref: dict[str, Gtk.Button | None] = {"btn": None}

    def set_archive(path: str, info: dict | None) -> None:
        archive_state["path"] = path
        if not path:
            arch_row.set_title("No archive selected")
            arch_row.set_subtitle(
                "Open a .tar.xz, .tar.gz, or .zip — UrStack look packs, icon themes, GTK themes, Plasma widgets, or wallpaper."
            )
            btn = install_btn_ref["btn"]
            if btn is not None:
                btn.set_sensitive(False)
            return
        kind = (info or {}).get("kind") or "archive"
        name = (info or {}).get("name") or Path(path).name
        summary = (info or {}).get("summary") or kind
        items = ", ".join((info or {}).get("items") or []) or kind
        arch_row.set_title(str(name))
        arch_row.set_subtitle(f"{summary} · {items} · {Path(path).name}")
        btn = install_btn_ref["btn"]
        if btn is not None:
            btn.set_sensitive(True)

    def pick_archive(*_a: object) -> None:
        dialog = Gtk.FileDialog(title="Open a theme archive")
        store, filt = _look_file_filters()
        try:
            dialog.set_filters(store)
            dialog.set_default_filter(filt)
        except Exception:  # noqa: BLE001
            pass

        def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = dialog.open_finish(result)
            except Exception:  # noqa: BLE001
                return
            if gfile is None:
                return
            path = gfile.get_path() or ""
            if not path:
                return
            try:
                info = look_engine.inspect_archive(Path(path)).as_dict()
            except look_engine.LookError as exc:
                set_archive("", None)
                arch_row.set_title("Could not read archive")
                arch_row.set_subtitle(str(exc))
                return
            if info.get("unsafe"):
                set_archive("", None)
                arch_row.set_title("Archive refused")
                arch_row.set_subtitle(str(info["unsafe"]))
                return
            set_archive(path, info)

        dialog.open(parent_win, None, on_done)

    open_btn.connect("clicked", pick_archive)

    outer.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    actions.add_css_class("fu-actions")
    save_btn = mk_btn(
        "Save look pack",
        "suggested-action pill fu-primary",
        "document-save-symbolic",
    )
    save_btn.set_hexpand(True)
    install_btn = mk_btn(
        "Install archive",
        "pill fu-secondary",
        pick_icon("folder-download-symbolic", "document-save-symbolic", "emblem-ok-symbolic"),
    )
    install_btn.set_hexpand(True)
    install_btn.set_sensitive(False)
    install_btn_ref["btn"] = install_btn

    def include_csv() -> str:
        return ",".join(k for k, sw in switches.items() if sw.get_active())

    def pick_save(*_a: object) -> None:
        dialog = Gtk.FileDialog(title="Save look pack")
        stamp = datetime.now().strftime("%Y-%m-%d")
        desk = str(snap_d.get("desktop") or "desktop")
        dialog.set_initial_name(f"urstack-look-{desk}-{stamp}.tar.xz")
        store, filt = _look_file_filters()
        try:
            dialog.set_filters(store)
            dialog.set_default_filter(filt)
        except Exception:  # noqa: BLE001
            pass

        def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                gfile = dialog.save_finish(result)
            except Exception:  # noqa: BLE001
                return
            if gfile is None:
                return
            path = gfile.get_path() or ""
            if not path:
                return
            on_export(path, include_csv())

        dialog.save(parent_win, None, on_done)

    def do_install(*_a: object) -> None:
        path = archive_state["path"]
        if not path:
            return
        on_install(path)

    save_btn.connect("clicked", pick_save)
    install_btn.connect("clicked", do_install)
    actions.append(save_btn)
    actions.append(install_btn)
    outer.append(pin_page_footer(actions))
    return outer


def _urstack_command() -> list[str]:
    root = Path(__file__).resolve().parents[2]
    local = root / "bin" / "urstack"
    if local.is_file() and os.access(local, os.X_OK):
        return [str(local)]
    for name in ("urstack", "stackup", "fedora-updates"):
        found = shutil_which(name)
        if found:
            return [found]
    return [str(local)]


# A health scan shells out to dnf/flatpak, which can stall indefinitely on an
# unreachable mirror. Without a cap the worker thread never finishes and the page
# spins forever.
HEALTH_SCAN_TIMEOUT = 300
# Grace period for a job to exit after it has closed stdout.
JOB_EXIT_TIMEOUT = 60
# How long a cancelled job gets to wind down between SIGTERM and SIGKILL.
JOB_CANCEL_GRACE = 5


def terminate_process_group(proc: subprocess.Popen | None) -> None:
    """Stop a running job and the children it spawned.

    A job is a shell script that shells out to rsync and tar, so signalling
    only the script would leave those copying in the background. The job is
    started in its own session, which gives the whole tree one process group
    to signal.

    Steps running under pkexec belong to root and cannot be signalled from an
    unprivileged process; those finish on their own. Killing the rest still
    stops the job from starting any new ones.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            # Only root-owned members left; the reader thread still sees EOF.
            return
        try:
            proc.wait(timeout=JOB_CANCEL_GRACE)
            return
        except subprocess.TimeoutExpired:
            continue


def run_health_scan_subprocess(status_path: str) -> bool:
    """Run a health scan into status_path. False if it failed or timed out."""
    root = str(Path(__file__).resolve().parents[2])
    try:
        subprocess.run(
            _urstack_command() + ["--health-scan", "--health-status", status_path],
            check=False,
            capture_output=True,
            text=True,
            timeout=HEALTH_SCAN_TIMEOUT,
            env={
                **os.environ,
                "URSTACK_ROOT": root,
                "STACKUP_ROOT": root,
                "FEDORA_UPDATES_ROOT": root,
                "URSTACK_EMBEDDED_PROGRESS": "1",
            },
        )
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


def run_workstation_rescan(config_file: str | Path) -> tuple[bool, list[str], str]:
    """Run detect --write-config without leaving the UI. Preserves backup_* prefs."""
    cfg_path = Path(config_file).expanduser()
    prev = read_config_map(cfg_path) if cfg_path.is_file() else {}
    root = Path(__file__).resolve().parents[2]
    cmd = _urstack_command() + ["--detect", "--write-config"]
    env = os.environ.copy()
    env["FEDORA_UPDATES_USER_CONFIG"] = str(cfg_path)
    env["FEDORA_UPDATES_ROOT"] = str(root)
    env["URSTACK_ROOT"] = str(root)
    env["STACKUP_ROOT"] = str(root)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [], str(exc)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scan failed").strip()
        return False, [], err[:300]

    # Preserve opt-in backup module + paths (detect leaves enable_backup=0)
    values = read_config_map(cfg_path)
    changed = False
    for key, val in prev.items():
        if key in {
            "enable_backup",
            "appearance",
            "autostart",
            "autostart_background",
            "scan_on_startup",
            "daily_check",
            "notifications",
            "apply_fw",
        } or key.startswith("backup_"):
            if values.get(key) != val:
                values[key] = val
                changed = True
    if changed:
        write_config_map(cfg_path, values)
        values = read_config_map(cfg_path)

    enabled = [
        key.removeprefix("enable_")
        for key, val in values.items()
        if key.startswith("enable_") and val == "1"
    ]
    return True, enabled, (proc.stdout or "").strip()


def mode_settings(args: argparse.Namespace) -> int:
    """Toggle update sources; Save writes config; Scan runs in-place."""

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title)
        toast = Adw.ToastOverlay()
        result = {"action": "close"}

        content = build_settings_content(
            args.config_file,
            on_rescan=None,
            on_saved=None,
            toast_overlay=toast,
        )
        toast.set_child(content)
        wrap_shell(win, args.title, toast, "Sources & behaviour")

        def on_close(*_a: object) -> bool:
            print(result["action"], flush=True)
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.settings", build)



def mode_catalog(args: argparse.Namespace) -> int:
    """Browse popular apps by category; multi-select → install-batch|file or close."""

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title)
        result = {"action": "close"}

        def finish(msg: str) -> None:
            result["action"] = msg
            win.close()

        content = build_catalog_content(
            args.status_file,
            on_install=finish,
            on_back=lambda: finish("close"),
            category=getattr(args, "category", "") or "",
        )
        wrap_shell(win, args.title, content, "Popular apps")

        def on_close(*_a: object) -> bool:
            print(result["action"], flush=True)
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.present()

    return run_app("com.local.urstack.catalog", build)


def _load_catalog_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.is_file():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split("|")
        if len(parts) < 8:
            continue
        rows.append(
            {
                "id": parts[0],
                "name": parts[1],
                "summary": parts[2],
                "category": parts[3],
                "category_id": parts[4],
                "method": parts[5],
                "package": parts[6],
                "installed": parts[7],
                "url": parts[8] if len(parts) > 8 else "",
                "badge": parts[9] if len(parts) > 9 else parts[5],
                "icon": parts[10] if len(parts) > 10 else "",
                "repo_hint": parts[11] if len(parts) > 11 else "",
                "user": (
                    parts[12]
                    if len(parts) > 12
                    else ("1" if parts[0].startswith("user-") else "0")
                ),
            }
        )
    return rows


_CATALOG_ICON_PLACEHOLDER = ""
_ICON_TEXTURE_CACHE: dict[tuple[str, int], object] = {}


class _CatalogItem(GObject.Object):
    """One Apps-grid row. GridView virtualizes these so off-screen cards are not built."""

    __gtype_name__ = "UrstackCatalogItem"

    def __init__(self, row: dict[str, str]) -> None:
        super().__init__()
        self.row = row


def _catalog_icon_placeholder() -> str:
    global _CATALOG_ICON_PLACEHOLDER
    if not _CATALOG_ICON_PLACEHOLDER:
        _CATALOG_ICON_PLACEHOLDER = pick_icon(
            "application-x-executable-symbolic", "applications-other-symbolic"
        )
    return _CATALOG_ICON_PLACEHOLDER


def _icon_texture_for_path(path: Path, pixel_size: int) -> object | None:
    key = (str(path), pixel_size)
    cached = _ICON_TEXTURE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(path), pixel_size, pixel_size, True
        )
    except Exception:  # noqa: BLE001
        return None
    if pix is None:
        return None
    try:
        tex = Gdk.Texture.new_for_pixbuf(pix)
    except Exception:  # noqa: BLE001
        tex = pix
    if len(_ICON_TEXTURE_CACHE) > 512:
        _ICON_TEXTURE_CACHE.clear()
    _ICON_TEXTURE_CACHE[key] = tex
    return tex


def _catalog_set_app_icon(img: Gtk.Image, row: dict[str, str], pixel_size: int) -> None:
    """Load a catalog logo into an existing image; ignore stale async results."""
    img.set_pixel_size(pixel_size)
    img.set_from_icon_name(_catalog_icon_placeholder())
    gen = int(getattr(img, "_icon_gen", 0)) + 1
    img._icon_gen = gen  # type: ignore[attr-defined]
    try:
        import app_icons as _app_icons
    except ImportError:
        return

    def apply_path(path: Path | None) -> bool:
        if int(getattr(img, "_icon_gen", 0)) != gen:
            return False
        if path is None or not path.is_file():
            return False
        tex = _icon_texture_for_path(path, pixel_size)
        if tex is None:
            return False
        try:
            img.set_from_paintable(tex)
        except Exception:  # noqa: BLE001
            try:
                img.set_from_pixbuf(tex)
            except Exception:  # noqa: BLE001
                return False
        return False

    local = _app_icons.icon_path_for_row(row)
    if local is not None:
        apply_path(local)
        return

    url = _app_icons.icon_url_for_row(row)
    if not url:
        return

    def on_done(path: Path | None) -> None:
        GLib.idle_add(apply_path, path)

    _app_icons.fetch_icon_async(url, on_done)


def _catalog_app_icon(row: dict[str, str], pixel_size: int = 32) -> Gtk.Image:
    """Real app logo from bundled catalog icons, with a download fallback."""
    img = Gtk.Image.new_from_icon_name(_catalog_icon_placeholder())
    img.set_pixel_size(pixel_size)
    img.set_valign(Gtk.Align.CENTER)
    _catalog_set_app_icon(img, row, pixel_size)
    return img


def method_badge_label(badge: str, method: str) -> tuple[str, str]:
    """Return (display label, css modifier) for install method chips."""
    raw = (badge or method or "").strip().lower()
    if raw in {"outside stores", "outside", "vendor", "direct", "external"}:
        return "Vendor", "vendor"
    if raw in {"windows", "windows-only"}:
        return "Windows", "windows"
    if raw in {"browser", "link"}:
        return "Link", "link"
    if raw == "flatpak":
        return "Flatpak", "flatpak"
    if raw == "dnf":
        return "DNF", "dnf"
    if raw == "snap":
        return "Snap", "snap"
    if raw in {"cursor_rpm", "rpm_url", "appimage", "script", "toolbox_tarball"}:
        return "Vendor", "vendor"
    return (raw.upper() or "App"), "outside"


def _find_navigation_view(widget: Gtk.Widget | None) -> Adw.NavigationView | None:
    w = widget
    for _ in range(48):
        if w is None:
            return None
        if isinstance(w, Adw.NavigationView):
            return w
        try:
            w = w.get_parent()
        except Exception:  # noqa: BLE001
            return None
    return None


def _widget_window(widget: Gtk.Widget | None) -> Gtk.Window | None:
    if widget is None:
        return None
    try:
        root = widget.get_root()
    except Exception:  # noqa: BLE001
        root = None
    return root if isinstance(root, Gtk.Window) else None


def _open_uri(uri: str, parent: Gtk.Window | None = None) -> None:
    uri = (uri or "").strip()
    if not uri:
        return
    try:
        Gio.AppInfo.launch_default_for_uri(uri, None)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        Gtk.show_uri(parent, uri, Gdk.CURRENT_TIME)
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        subprocess.Popen(
            ["xdg-open", uri],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        pass


def _catalog_install_choice(row: dict[str, str]) -> str:
    return "|".join(
        [
            "install",
            row.get("method", ""),
            row.get("package", ""),
            row.get("name", ""),
            row.get("url", ""),
        ]
    )


_UNINSTALLABLE_METHODS = frozenset(
    {"flatpak", "dnf", "snap", "cursor_rpm", "appimage", "rpm_url"}
)


def _catalog_can_uninstall(row: dict[str, str]) -> bool:
    if row.get("installed") != "1":
        return False
    return (row.get("method") or "").strip() in _UNINSTALLABLE_METHODS


def _catalog_uninstall_choice(row: dict[str, str]) -> str:
    method = (row.get("method") or "").strip()
    package = (row.get("package") or "").strip()
    if method == "cursor_rpm":
        package = package or "cursor"
    return "|".join(
        [
            "uninstall",
            method,
            package,
            row.get("name", ""),
            row.get("url", ""),
        ]
    )


def _app_source_detail(row: dict[str, str]) -> str:
    method = (row.get("method") or "").strip()
    package = (row.get("package") or "").strip()
    label, _css = method_badge_label(row.get("badge", ""), method)
    if method == "flatpak":
        return f"Flathub Flatpak ({package})" if package else "Flathub Flatpak"
    if method == "dnf":
        return f"Fedora / RPM package ({package})" if package else "DNF package"
    if method == "snap":
        return f"Snap ({package})" if package else "Snap"
    if method == "appimage":
        return "AppImage download into ~/Applications"
    if method in {"cursor_rpm", "rpm_url"}:
        return "Vendor RPM"
    if method == "script":
        return "Vendor install script"
    if method == "toolbox_tarball":
        return "Vendor tarball installer"
    if method == "browser":
        return "Opens the vendor download page"
    return label


def _app_primary_action(row: dict[str, str]) -> tuple[str, str]:
    """Return (button label, icon name) for the details-page primary action."""
    if row.get("installed") == "1":
        return "", ""
    method = (row.get("method") or "").strip()
    if method in {"browser", "link"}:
        return "Open download page", pick_icon("document-open-symbolic", "web-browser-symbolic")
    return f"Install {row.get('name') or 'app'}", pick_icon(
        "software-install-symbolic", "emblem-ok-symbolic"
    )


def _detail_fact_row(title: str, value: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=value)
    try:
        row.set_subtitle_selectable(True)
    except Exception:  # noqa: BLE001
        pass
    try:
        row.set_subtitle_lines(4)
    except AttributeError:
        pass
    return row


def _app_meta_for_row(app_row: dict[str, str]) -> dict:
    try:
        import app_meta as _app_meta
    except ImportError:
        return {}
    meta = _app_meta.meta_for_row(app_row)
    return dict(meta) if isinstance(meta, dict) else {}


def _app_detail_link(app_row: dict[str, str], meta: dict) -> str:
    return (
        (app_row.get("url") or "").strip()
        or str(meta.get("homepage") or "").strip()
    )


def _app_detail_byline(app_row: dict[str, str], meta: dict) -> str:
    bits: list[str] = []
    developer = str(meta.get("developer") or "").strip()
    if developer:
        bits.append(developer)
    license_ = str(meta.get("license") or "").strip()
    if license_ and license_.lower() not in {"unknown", "none"}:
        bits.append(license_)
    version = str(meta.get("version") or "").strip()
    if version:
        bits.append(version)
    if meta.get("verified"):
        bits.append("Verified on Flathub")
    if not bits:
        source = _app_source_detail(app_row)
        if source:
            bits.append(source)
    return " · ".join(bits)


def _append_detail_shot(
    strip: Gtk.Box,
    path: Path,
    full_url: str,
    parent: Gtk.Window | None,
    *,
    gallery: list[dict[str, str]] | None = None,
    index: int = 0,
) -> None:
    try:
        pix = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), -1, 148, True)
        if pix is None:
            return
        try:
            tex = Gdk.Texture.new_for_pixbuf(pix)
            picture = Gtk.Picture.new_for_paintable(tex)
            try:
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            picture = Gtk.Image.new_from_pixbuf(pix)
        picture.add_css_class("fu-shot")
        picture.set_size_request(int(pix.get_width()), 148)
        picture.set_halign(Gtk.Align.START)
        btn = Gtk.Button()
        btn.set_has_frame(False)
        btn.add_css_class("flat")
        btn.add_css_class("fu-shot-btn")
        btn.set_child(picture)
        btn.set_tooltip_text("View screenshot")
        shots = gallery if gallery else [{"thumb": "", "full": full_url}]
        btn.connect(
            "clicked",
            lambda *_a, i=index, g=shots, p=path: _show_shot_lightbox(parent, g, i, p),
        )
        strip.append(btn)
    except Exception:  # noqa: BLE001
        return


def _shot_picture_from_path(path: Path) -> Gtk.Widget | None:
    try:
        picture = Gtk.Picture.new_for_filename(str(path))
        try:
            picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        except Exception:  # noqa: BLE001
            pass
        try:
            picture.set_can_shrink(True)
        except Exception:  # noqa: BLE001
            pass
        picture.set_hexpand(True)
        picture.set_vexpand(True)
        picture.add_css_class("fu-shot-view-picture")
        return picture
    except Exception:  # noqa: BLE001
        try:
            pix = GdkPixbuf.Pixbuf.new_from_file(str(path))
            if pix is None:
                return None
            img = Gtk.Image.new_from_pixbuf(pix)
            img.set_hexpand(True)
            img.set_vexpand(True)
            return img
        except Exception:  # noqa: BLE001
            return None


def _show_shot_lightbox(
    parent: Gtk.Window | None,
    gallery: list[dict[str, str]],
    index: int,
    thumb_path: Path | None = None,
) -> None:
    """Show a screenshot inside UrStack instead of opening the browser."""
    shots = [s for s in gallery if isinstance(s, dict) and (s.get("full") or s.get("thumb"))]
    if not shots:
        if thumb_path is not None:
            shots = [{"thumb": str(thumb_path), "full": str(thumb_path)}]
        else:
            return
    index = max(0, min(int(index), len(shots) - 1))
    state = {"i": index, "alive": True, "token": 0, "thumb": thumb_path}

    def shot_url(i: int) -> str:
        item = shots[i]
        return str(item.get("full") or item.get("thumb") or "").strip()

    try:
        import app_meta as _app_meta
    except ImportError:
        _app_meta = None  # type: ignore[assignment]

    title_lab = Gtk.Label(label="")
    try:
        title_lab.add_css_class("heading")
    except Exception:  # noqa: BLE001
        pass

    stage = Gtk.Overlay()
    stage.add_css_class("fu-shot-view")
    stage.set_hexpand(True)
    stage.set_vexpand(True)
    picture_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    picture_host.set_hexpand(True)
    picture_host.set_vexpand(True)
    picture_host.set_halign(Gtk.Align.FILL)
    picture_host.set_valign(Gtk.Align.FILL)
    stage.set_child(picture_host)

    spinner = Gtk.Spinner()
    spinner.set_halign(Gtk.Align.CENTER)
    spinner.set_valign(Gtk.Align.CENTER)
    spinner.set_size_request(36, 36)
    stage.add_overlay(spinner)

    prev_btn = Gtk.Button.new_from_icon_name("go-previous-symbolic")
    next_btn = Gtk.Button.new_from_icon_name("go-next-symbolic")
    prev_btn.add_css_class("flat")
    next_btn.add_css_class("flat")
    prev_btn.set_tooltip_text("Previous screenshot")
    next_btn.set_tooltip_text("Next screenshot")
    nav_visible = len(shots) > 1
    prev_btn.set_visible(nav_visible)
    next_btn.set_visible(nav_visible)

    def set_picture(path: Path | None, loading: bool) -> None:
        while picture_host.get_first_child() is not None:
            picture_host.remove(picture_host.get_first_child())
        if path is not None:
            widget = _shot_picture_from_path(path)
            if widget is not None:
                picture_host.append(widget)
        spinner.set_visible(loading)
        if loading:
            spinner.start()
        else:
            spinner.stop()

    def show_index(i: int) -> None:
        if not state["alive"]:
            return
        state["i"] = i
        state["token"] += 1
        token = state["token"]
        n = len(shots)
        title_lab.set_label(f"Screenshot {i + 1} of {n}" if n > 1 else "Screenshot")
        prev_btn.set_sensitive(i > 0)
        next_btn.set_sensitive(i < n - 1)
        url = shot_url(i)
        thumb_url = str(shots[i].get("thumb") or "").strip()
        full_cached = None
        thumb_cached = None
        if _app_meta is not None:
            if url:
                full_cached = _app_meta.cached_shot_path(url)
            if thumb_url:
                thumb_cached = _app_meta.cached_shot_path(thumb_url)
        if thumb_cached is None and i == index:
            thumb_cached = state.get("thumb")
        preview = full_cached or thumb_cached
        fetching = bool(_app_meta is not None and url and full_cached is None)
        set_picture(preview if isinstance(preview, Path) else None, loading=fetching)
        if not fetching:
            return

        def on_full(path: Path | None, t: int = token) -> bool:
            if not state["alive"] or t != state["token"]:
                return False
            if path is not None:
                set_picture(path, loading=False)
            elif isinstance(preview, Path):
                set_picture(preview, loading=False)
            else:
                spinner.stop()
                spinner.set_visible(False)
            return False

        _app_meta.fetch_shot_async(url, lambda p: GLib.idle_add(on_full, p))

    def step(delta: int) -> None:
        nxt = state["i"] + delta
        if 0 <= nxt < len(shots):
            show_index(nxt)

    prev_btn.connect("clicked", lambda *_: step(-1))
    next_btn.connect("clicked", lambda *_: step(1))

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scrolled.set_hexpand(True)
    scrolled.set_vexpand(True)
    scrolled.set_child(stage)

    keys = Gtk.EventControllerKey()

    def on_key(
        _c: Gtk.EventControllerKey, keyval: int, _code: int, _state: Gdk.ModifierType
    ) -> bool:
        if keyval in {Gdk.KEY_Left, Gdk.KEY_KP_Left}:
            step(-1)
            return True
        if keyval in {Gdk.KEY_Right, Gdk.KEY_KP_Right}:
            step(1)
            return True
        return False

    keys.connect("key-pressed", on_key)

    try:
        dialog = Adw.Dialog()
        try:
            dialog.set_content_width(1100)
            dialog.set_content_height(740)
        except Exception:  # noqa: BLE001
            pass
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(title_lab)
        header.pack_start(prev_btn)
        header.pack_end(next_btn)
        toolbar.add_top_bar(header)
        toolbar.set_content(scrolled)
        dialog.set_child(toolbar)
        try:
            dialog.connect("closed", lambda *_: state.__setitem__("alive", False))
        except Exception:  # noqa: BLE001
            pass
        try:
            dialog.add_controller(keys)
        except Exception:  # noqa: BLE001
            scrolled.add_controller(keys)
        show_index(index)
        dialog.present(parent)
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        dialog = Adw.AlertDialog(heading="Screenshot", body="")
        dialog.add_response("ok", "Close")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_row.set_halign(Gtk.Align.CENTER)
        header_row.append(prev_btn)
        header_row.append(title_lab)
        header_row.append(next_btn)
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        wrap.append(header_row)
        wrap.append(scrolled)
        scrolled.set_min_content_height(420)
        scrolled.set_min_content_width(640)
        dialog.set_extra_child(wrap)
        try:
            wrap.add_controller(keys)
        except Exception:  # noqa: BLE001
            pass
        show_index(index)
        dialog.present(parent)
    except Exception:  # noqa: BLE001
        if thumb_path is not None:
            _open_uri(shot_url(index), parent)


def build_app_detail_content(
    app_row: dict[str, str],
    *,
    on_install: Callable[[], None] | None = None,
    on_open_url: Callable[[str], None] | None = None,
    on_uninstall: Callable[[], None] | None = None,
    parent_win: Gtk.Window | None = None,
) -> Gtk.Widget:
    """Full-page / dialog body for one catalog app."""
    name = (app_row.get("name") or "App").strip() or "App"
    catalog_summary = (app_row.get("summary") or "").strip()
    installed = app_row.get("installed") == "1"
    package = (app_row.get("package") or "").strip()
    category = (app_row.get("category") or "").strip()
    repo_hint = (app_row.get("repo_hint") or "").strip()
    meta = _app_meta_for_row(app_row)
    summary = str(meta.get("summary") or "").strip() or catalog_summary

    main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    main.set_hexpand(True)
    main.set_vexpand(True)
    main.set_margin_start(PAGE_SIDE_PAD)
    main.set_margin_end(PAGE_SIDE_PAD)

    hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
    hero.add_css_class("fu-page-hero")
    hero.set_hexpand(True)

    logo = _catalog_app_icon(app_row, 72)
    logo.set_valign(Gtk.Align.START)
    hero.append(logo)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    texts.set_hexpand(True)
    title = Gtk.Label(label=name, xalign=0.0, wrap=True)
    title.add_css_class("fu-hero-title")
    texts.append(title)
    if summary:
        sub = Gtk.Label(label=summary, xalign=0.0, wrap=True)
        sub.add_css_class("fu-hero-sub")
        sub.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        texts.append(sub)
    byline = Gtk.Label(label=_app_detail_byline(app_row, meta), xalign=0.0, wrap=True)
    byline.add_css_class("fu-app-byline")
    byline.set_visible(bool(byline.get_text()))
    texts.append(byline)
    hero.append(texts)

    status = Gtk.Label(label="Installed" if installed else "Available")
    status.add_css_class("fu-badge")
    status.add_css_class("fu-badge-ok" if installed else "fu-badge")
    status.set_valign(Gtk.Align.START)
    hero.append(status)
    main.append(hero)

    scrolled, _clamp, box = page_scroll_body(spacing=14)
    box.set_margin_bottom(8)

    shots_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    shots_host.set_visible(False)
    shot_strip: list[Gtk.Box] = []
    box.append(shots_host)

    about_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    about_host.set_visible(False)
    about_lab = Gtk.Label(label="", xalign=0.0, wrap=True, selectable=True)
    about_lab.add_css_class("fu-app-desc")
    about_lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    about_lab.set_margin_start(16)
    about_lab.set_margin_end(16)
    about_host.append(page_section_label("About"))
    about_host.append(about_lab)
    box.append(about_host)

    facts_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    box.append(facts_host)

    def link_handler(uri: str) -> None:
        if on_open_url is not None:
            on_open_url(uri)
        else:
            _open_uri(uri, parent_win)

    def add_link_row(group: Adw.PreferencesGroup, title: str, uri: str) -> None:
        if not uri:
            return
        row = _detail_fact_row(title, uri)
        try:
            row.set_activatable(True)
        except Exception:  # noqa: BLE001
            pass
        row.connect("activated", lambda *_a, u=uri: link_handler(u))
        open_btn = Gtk.Button.new_from_icon_name(
            pick_icon("adw-external-link-symbolic", "web-browser-symbolic")
        )
        open_btn.add_css_class("flat")
        open_btn.set_valign(Gtk.Align.CENTER)
        open_btn.set_tooltip_text("Open link")
        open_btn.connect("clicked", lambda *_a, u=uri: link_handler(u))
        row.add_suffix(open_btn)
        group.add(row)

    def fill_facts(current: dict) -> None:
        while facts_host.get_first_child() is not None:
            facts_host.remove(facts_host.get_first_child())
        group = Adw.PreferencesGroup(title="Details")
        group.add(_detail_fact_row("Category", category or "—"))
        group.add(_detail_fact_row("Source", _app_source_detail(app_row)))
        if package:
            group.add(_detail_fact_row("Package", package))
        developer = str(current.get("developer") or "").strip()
        if developer:
            group.add(_detail_fact_row("Developer", developer))
        license_ = str(current.get("license") or "").strip()
        if license_:
            group.add(_detail_fact_row("License", license_))
        version = str(current.get("version") or "").strip()
        if version:
            group.add(_detail_fact_row("Version", version))
        if repo_hint:
            group.add(_detail_fact_row("Note", repo_hint))
        homepage = _app_detail_link(app_row, current)
        add_link_row(group, "Website", homepage)
        add_link_row(group, "Help", str(current.get("help") or "").strip())
        add_link_row(group, "Donate", str(current.get("donation") or "").strip())
        add_link_row(group, "Bugs", str(current.get("bugtracker") or "").strip())
        facts_host.append(group)

    loaded_shots: set[str] = set()

    def fill_shots(current: dict) -> None:
        shots = current.get("screenshots") or []
        if not isinstance(shots, list) or not shots:
            return
        try:
            import app_meta as _app_meta
        except ImportError:
            return
        if not shot_strip:
            shots_host.append(page_section_label("Screenshots"))
            shot_scroll = Gtk.ScrolledWindow()
            shot_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
            shot_scroll.set_hexpand(True)
            strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            strip.add_css_class("fu-shot-strip")
            strip.set_margin_start(12)
            strip.set_margin_end(12)
            shot_scroll.set_child(strip)
            shots_host.append(shot_scroll)
            shot_strip.append(strip)
        strip = shot_strip[0]
        shots_host.set_visible(True)

        gallery: list[dict[str, str]] = []
        for shot in shots[:5]:
            if not isinstance(shot, dict):
                continue
            thumb = str(shot.get("thumb") or shot.get("full") or "").strip()
            full = str(shot.get("full") or thumb).strip()
            if thumb:
                gallery.append({"thumb": thumb, "full": full})
        if not gallery:
            return

        def on_shot(path: Path | None, full: str, idx: int) -> bool:
            if path is None or str(path) in loaded_shots:
                return False
            loaded_shots.add(str(path))
            _append_detail_shot(
                strip, path, full, parent_win, gallery=gallery, index=idx
            )
            return False

        for idx, item in enumerate(gallery):
            _app_meta.fetch_shot_async(
                item["thumb"],
                lambda p, u=item["full"], i=idx: GLib.idle_add(on_shot, p, u, i),
            )

    def apply_meta(current: dict | None) -> bool:
        if not isinstance(current, dict):
            return False
        meta.update(current)
        byline.set_label(_app_detail_byline(app_row, meta))
        byline.set_visible(bool(byline.get_text()))
        desc = str(current.get("description") or meta.get("description") or "").strip()
        if desc:
            about_lab.set_label(desc)
            about_host.set_visible(True)
        fill_facts(meta)
        fill_shots(meta)
        return False

    fill_facts(meta)
    if str(meta.get("description") or "").strip():
        about_lab.set_label(str(meta.get("description") or "").strip())
        about_host.set_visible(True)
    fill_shots(meta)

    try:
        import app_meta as _app_meta
    except ImportError:
        _app_meta = None  # type: ignore[assignment]
    if _app_meta is not None and not (meta.get("description") or meta.get("screenshots")):
        _app_meta.fetch_meta_async(
            app_row,
            lambda m: GLib.idle_add(apply_meta, m),
        )

    main.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    actions.add_css_class("fu-actions")
    primary_label, primary_icon = _app_primary_action(app_row)
    method = (app_row.get("method") or "").strip()
    homepage = _app_detail_link(app_row, meta)
    if primary_label and method in {"browser", "link"} and homepage:
        open_btn = mk_btn(
            primary_label,
            "suggested-action pill fu-primary",
            primary_icon,
        )
        open_btn.set_hexpand(True)
        open_btn.connect("clicked", lambda *_: link_handler(homepage))
        actions.append(open_btn)
    elif primary_label and on_install is not None:
        install_btn = mk_btn(
            primary_label,
            "suggested-action pill fu-primary",
            primary_icon,
        )
        install_btn.set_hexpand(True)
        install_btn.connect("clicked", lambda *_: on_install())
        actions.append(install_btn)
    show_visit = bool(homepage and on_open_url is not None)
    if show_visit and primary_label and method in {"browser", "link"}:
        show_visit = False
    if show_visit:
        visit = mk_btn("Visit website", "pill fu-secondary", "web-browser-symbolic")
        visit.set_hexpand(True)
        visit.connect("clicked", lambda *_: on_open_url(homepage))
        actions.append(visit)
    if on_uninstall is not None:
        uninstall_btn = mk_btn(
            "Uninstall",
            "pill destructive-action",
            pick_icon("user-trash-symbolic", "edit-delete-symbolic"),
        )
        uninstall_btn.set_hexpand(True)
        uninstall_btn.set_tooltip_text("Remove this package from the computer")
        uninstall_btn.connect("clicked", lambda *_: on_uninstall())
        actions.append(uninstall_btn)
    if actions.get_first_child() is not None:
        main.append(pin_page_footer(actions))
    return main


def _show_app_details(
    source: Gtk.Widget,
    app_row: dict[str, str],
    on_install: Callable[[str], None],
) -> None:
    """Push a details page in the shell, or show a dialog in standalone catalog mode."""
    parent = _widget_window(source)
    name = (app_row.get("name") or "App").strip() or "App"
    aid = (app_row.get("id") or name).strip() or "app"
    closer: dict[str, Callable[[], None] | None] = {"fn": None}
    closed = {"v": False}

    def close_detail() -> None:
        if closed["v"]:
            return
        closed["v"] = True
        fn = closer.get("fn")
        if fn is not None:
            fn()

    def do_install() -> None:
        on_install(_catalog_install_choice(app_row))

    def do_open_url(uri: str = "") -> None:
        _open_uri(uri or _app_detail_link(app_row, _app_meta_for_row(app_row)), parent)

    def confirm_uninstall() -> None:
        try:
            dialog = Adw.AlertDialog(
                heading=f"Uninstall {name}?",
                body="This removes the package from this computer.",
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("uninstall", "Uninstall")
            dialog.set_response_appearance(
                "uninstall", Adw.ResponseAppearance.DESTRUCTIVE
            )
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")

            def on_resp(_d: Adw.AlertDialog, response: str) -> None:
                if response == "uninstall":
                    close_detail()
                    on_install(_catalog_uninstall_choice(app_row))

            dialog.connect("response", on_resp)
            dialog.present(parent)
            return
        except Exception:  # noqa: BLE001
            close_detail()
            on_install(_catalog_uninstall_choice(app_row))

    content = build_app_detail_content(
        app_row,
        on_install=None if app_row.get("installed") == "1" else do_install,
        on_open_url=do_open_url,
        on_uninstall=confirm_uninstall if _catalog_can_uninstall(app_row) else None,
        parent_win=parent,
    )

    nav = _find_navigation_view(source)
    if nav is not None:
        tag = f"app-{aid}"
        try:
            existing = nav.find_page(tag)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None:
            try:
                nav.pop_to_page(existing)
                return
            except Exception:  # noqa: BLE001
                pass
        page = make_nav_page(name, content, tag=tag)
        nav.push(page)
        closer["fn"] = lambda n=nav: bool(n.pop())
        return

    try:
        dialog = Adw.Dialog()
        try:
            dialog.set_title(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            dialog.set_content_width(560)
            dialog.set_content_height(640)
        except Exception:  # noqa: BLE001
            pass
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)
        toolbar.set_content(content)
        dialog.set_child(toolbar)
        closer["fn"] = dialog.close
        dialog.present(parent)
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        dialog = Adw.AlertDialog(heading=name, body="")
        dialog.add_response("ok", "Close")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.set_extra_child(content)
        closer["fn"] = dialog.close
        dialog.present(parent)
    except Exception:  # noqa: BLE001
        pass


def _show_add_user_app_dialog(
    parent: Gtk.Window | None,
    existing_packages: set[str],
    on_added: Callable[[dict[str, str]], None],
) -> None:
    """Add a Flathub / DNF / Snap listing to the personal overlay."""
    methods = (
        ("flatpak", "Flathub ID", "org.mozilla.firefox"),
        ("dnf", "DNF package", "vlc"),
        ("snap", "Snap name", "vlc"),
    )
    selected = {"i": 0}

    def current_method() -> tuple[str, str, str]:
        return methods[selected["i"]]

    hint = Gtk.Label(
        label="Flathub app ID, DNF package name, or Snap name. Extra remotes and vendor scripts are not allowed.",
        xalign=0.0,
        wrap=True,
    )
    hint.add_css_class("dim-label")
    hint.set_wrap_mode(Pango.WrapMode.WORD_CHAR)

    error = Gtk.Label(label="", xalign=0.0, wrap=True)
    error.add_css_class("error")
    error.set_visible(False)

    form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    form.set_margin_start(16)
    form.set_margin_end(16)
    form.set_margin_top(8)
    form.set_margin_bottom(12)
    form.append(hint)

    try:
        labels = Gtk.StringList.new([m[1] for m in methods])
        combo = Adw.ComboRow(title="Source", model=labels)
        pkg = Adw.EntryRow(title="Package or app ID")
        name = Adw.EntryRow(title="Display name (optional)")
        group = Adw.PreferencesGroup()
        group.add(combo)
        group.add(pkg)
        group.add(name)
        form.append(group)

        def on_method(*_a: object) -> None:
            idx = int(combo.get_selected())
            if 0 <= idx < len(methods):
                selected["i"] = idx
                try:
                    pkg.set_title(methods[idx][1])
                except Exception:  # noqa: BLE001
                    pass

        combo.connect("notify::selected", on_method)
        get_pkg = pkg.get_text
        get_name = name.get_text
    except Exception:  # noqa: BLE001
        combo_box = Gtk.DropDown.new_from_strings([m[1] for m in methods])
        pkg_entry = Gtk.Entry()
        pkg_entry.set_placeholder_text("org.mozilla.firefox")
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("Optional name")
        form.append(combo_box)
        form.append(pkg_entry)
        form.append(name_entry)

        def on_drop(*_a: object) -> None:
            idx = int(combo_box.get_selected())
            if 0 <= idx < len(methods):
                selected["i"] = idx
                pkg_entry.set_placeholder_text(methods[idx][2])

        combo_box.connect("notify::selected", on_drop)
        get_pkg = pkg_entry.get_text
        get_name = name_entry.get_text

    form.append(error)

    def submit() -> bool:
        method = current_method()[0]
        try:
            app = user_catalog.add_app(
                method,
                get_pkg().strip(),
                get_name().strip(),
                existing_packages=existing_packages,
            )
        except ValueError as exc:
            error.set_label(str(exc))
            error.set_visible(True)
            return False
        on_added(user_catalog.as_catalog_row(app))
        return True

    try:
        dialog = Adw.Dialog()
        try:
            dialog.set_title("Add app")
            dialog.set_content_width(460)
        except Exception:  # noqa: BLE001
            pass
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        cancel = Gtk.Button(label="Cancel")
        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("suggested-action")
        header.pack_start(cancel)
        header.pack_end(add_btn)
        toolbar.add_top_bar(header)
        toolbar.set_content(form)
        dialog.set_child(toolbar)
        cancel.connect("clicked", lambda *_: dialog.close())

        def on_add(*_a: object) -> None:
            if submit():
                dialog.close()

        add_btn.connect("clicked", on_add)
        dialog.present(parent)
        return
    except Exception:  # noqa: BLE001
        pass

    try:
        dialog = Adw.AlertDialog(
            heading="Add app",
            body="Flathub ID, DNF package, or Snap name only.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_default_response("add")
        dialog.set_close_response("cancel")
        dialog.set_extra_child(form)

        def on_resp(_d: Adw.AlertDialog, response: str) -> None:
            if response == "add":
                submit()

        dialog.connect("response", on_resp)
        dialog.present(parent)
    except Exception:  # noqa: BLE001
        pass


def build_catalog_content(
    status_file: str,
    *,
    on_install: Callable[[str], None],
    on_back: Callable[[], None] | None = None,
    category: str = "",
) -> Gtk.Widget:
    """Apps browser: search, category chips, and a filtered catalog."""
    import tempfile

    CAT_ORDER = [
        "mine",
        "added",
        "browsers",
        "communication",
        "media",
        "productivity",
        "developer",
        "cli",
        "graphics",
        "gaming",
        "utilities",
        "microsoft",
        "pro-tools",
        "selfhosted",
        "direct",
    ]
    CAT_ICONS = {
        "": pick_icon("applications-all-symbolic", "view-app-grid-symbolic"),
        "mine": pick_icon("starred-symbolic", "user-bookmarks-symbolic"),
        "added": pick_icon("list-add-symbolic", "folder-new-symbolic"),
        # Prefer icons that exist on both GNOME and KDE/Breeze themes
        "browsers": pick_icon("applications-internet-symbolic", "web-browser-symbolic"),
        "communication": pick_icon("system-users-symbolic", "user-available-symbolic"),
        "media": pick_icon("applications-multimedia-symbolic", "folder-music-symbolic"),
        "productivity": pick_icon("applications-office-symbolic", "document-open-symbolic"),
        "developer": pick_icon(
            "applications-development-symbolic", "applications-engineering-symbolic"
        ),
        "cli": pick_icon(
            "utilities-terminal-symbolic", "application-x-terminal-symbolic"
        ),
        "graphics": "applications-graphics-symbolic",
        "gaming": "applications-games-symbolic",
        "utilities": "applications-utilities-symbolic",
        "microsoft": pick_icon("applications-other-symbolic", "input-keyboard-symbolic"),
        "pro-tools": pick_icon(
            "applications-science-symbolic", "applications-engineering-symbolic"
        ),
        "selfhosted": "network-server-symbolic",
        "direct": "folder-download-symbolic",
    }
    CAT_LABELS = {
        "mine": "My apps",
        "added": "Added by you",
        "browsers": "Browsers",
        "communication": "Communication",
        "media": "Media",
        "productivity": "Productivity",
        "developer": "Developer",
        "cli": "CLIs & tools",
        "graphics": "Graphics",
        "gaming": "Gaming",
        "utilities": "Utilities",
        "microsoft": "Microsoft",
        "pro-tools": "Pro tools",
        "selfhosted": "Self-hosted",
        "direct": "Direct",
    }
    METHOD_CHOICES = [
        ("all", "Any source"),
        ("flatpak", "Flatpak"),
        ("dnf", "DNF"),
        ("vendor", "Vendor / Link"),
    ]

    rows = _load_catalog_rows(Path(status_file))
    filter_cat = {"id": category or ""}
    filter_query = {"q": ""}
    # status: all | available | installed
    filter_status = {"v": "all"}
    # method: all | flatpak | dnf | vendor
    filter_method = {"v": "all"}
    selected: dict[str, dict[str, str]] = {}
    visible_checks: list[tuple[dict[str, str], Gtk.CheckButton]] = []

    cat_counts: dict[str, int] = {}
    cat_names: dict[str, str] = {}
    ordered_ids: list[str] = []

    def refresh_cat_state() -> None:
        cat_counts.clear()
        cat_names.clear()
        cat_names["mine"] = CAT_LABELS["mine"]
        cat_names["added"] = CAT_LABELS["added"]
        installed_n = 0
        for r in rows:
            if r.get("installed") == "1":
                installed_n += 1
            cid = r["category_id"]
            if cid == "mine":
                cid = "added"
            if not cid:
                continue
            cat_counts[cid] = cat_counts.get(cid, 0) + 1
            if cid == "added":
                cat_names[cid] = CAT_LABELS["added"]
            else:
                cat_names.setdefault(cid, r["category"])
        cat_counts["mine"] = installed_n
        ordered_ids.clear()
        for cid in CAT_ORDER:
            if cid == "mine" or cid in cat_counts:
                ordered_ids.append(cid)
        ordered_ids.extend(sorted(c for c in cat_counts if c not in CAT_ORDER))

    refresh_cat_state()

    def cat_chip_label(cid: str) -> str:
        if not cid:
            return "All"
        return (
            CAT_LABELS.get(cid)
            or (cat_names.get(cid) or "").strip()
            or cid.replace("-", " ").title()
            or "Other"
        )

    def cat_full_label(cid: str) -> str:
        if not cid:
            return "All apps"
        return (cat_names.get(cid) or "").strip() or cat_chip_label(cid)

    main = page_frame()

    main.append(
        page_hero(
            "",
            "",
            "Browse the catalog",
            "Desktop apps, plus CLIs and other developer tools that aren't store apps.",
            heading="Apps",
            icon_name=page_icon("apps"),
        )
    )

    # Chrome stays outside the rebuild scroll so search focus/state survives refreshes
    chrome = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    chrome.add_css_class("fu-apps-chrome")

    cat_rail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
    cat_rail.add_css_class("fu-cat-rail")
    cat_rail.set_halign(Gtk.Align.START)
    cat_rail.set_valign(Gtk.Align.CENTER)
    cat_rail.set_hexpand(False)
    cat_scroll = Gtk.ScrolledWindow()
    cat_scroll.add_css_class("fu-cat-scroll")
    cat_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
    cat_scroll.set_hexpand(True)
    cat_scroll.set_vexpand(False)
    try:
        cat_scroll.set_propagate_natural_height(True)
        cat_scroll.set_overlay_scrolling(True)
        cat_scroll.set_kinetic_scrolling(True)
    except Exception:  # noqa: BLE001
        pass
    cat_scroll.set_child(cat_rail)

    cat_btns: dict[str, Gtk.ToggleButton] = {}
    cat_guard = {"busy": True}
    search_guard = {"busy": False}
    method_guard = {"busy": False}

    def sync_cat_btns() -> None:
        cat_guard["busy"] = True
        try:
            current = filter_cat["id"]
            for key, btn in cat_btns.items():
                btn.set_active(key == current)
        finally:
            cat_guard["busy"] = False

    def on_cat_toggle(btn: Gtk.ToggleButton, key: str) -> None:
        if cat_guard["busy"]:
            return
        if btn.get_active():
            filter_cat["id"] = key
            sync_cat_btns()
            rebuild_list()
            return
        if filter_cat["id"] != key:
            return
        # Second click on a category returns to All; All itself stays selected.
        if key:
            filter_cat["id"] = ""
            sync_cat_btns()
            rebuild_list()
            return
        cat_guard["busy"] = True
        try:
            btn.set_active(True)
        finally:
            cat_guard["busy"] = False

    def add_cat_pill(cid: str) -> None:
        label = cat_chip_label(cid)
        inner = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        icon = Gtk.Image.new_from_icon_name(
            CAT_ICONS.get(
                cid,
                pick_icon("application-x-executable-symbolic", "applications-other-symbolic"),
            )
        )
        icon.set_pixel_size(16)
        inner.append(icon)
        name = Gtk.Label(label=label)
        name.set_single_line_mode(True)
        inner.append(name)
        btn = Gtk.ToggleButton()
        btn.set_child(inner)
        btn.add_css_class("flat")
        btn.add_css_class("fu-cat-pill")
        btn.set_active(cid == filter_cat["id"])
        btn.set_tooltip_text(cat_full_label(cid))
        btn.connect("toggled", lambda b, k=cid: on_cat_toggle(b, k))
        cat_btns[cid] = btn
        cat_rail.append(btn)

    def rebuild_cat_rail() -> None:
        refresh_cat_state()
        cat_guard["busy"] = True
        try:
            child = cat_rail.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                cat_rail.remove(child)
                child = nxt
            cat_btns.clear()
            add_cat_pill("")
            for cid in ordered_ids:
                add_cat_pill(cid)
        finally:
            cat_guard["busy"] = False

    rebuild_cat_rail()

    search = Gtk.SearchEntry()
    search.set_placeholder_text("Search apps, packages, or categories…")
    search.add_css_class("fu-apps-search")
    search.set_hexpand(True)
    search.set_valign(Gtk.Align.CENTER)
    search.set_size_request(220, -1)
    search.set_tooltip_text("Search by name, summary, package, or category (Ctrl+F)")

    filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    filter_row.add_css_class("fu-filter-toolbar")
    filter_row.set_hexpand(True)
    filter_row.set_valign(Gtk.Align.CENTER)

    status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    status_box.add_css_class("linked")
    status_box.set_valign(Gtk.Align.CENTER)
    status_box.set_tooltip_text("Show all apps, only ones you can install, or already installed")

    status_btns: dict[str, Gtk.ToggleButton] = {}
    status_guard = {"busy": False}

    def sync_status_btns() -> None:
        status_guard["busy"] = True
        try:
            for key, btn in status_btns.items():
                btn.set_active(key == filter_status["v"])
        finally:
            status_guard["busy"] = False

    def on_status_toggle(btn: Gtk.ToggleButton, key: str) -> None:
        if status_guard["busy"]:
            return
        if btn.get_active():
            filter_status["v"] = key
            sync_status_btns()
            rebuild_list()
        elif filter_status["v"] == key:
            status_guard["busy"] = True
            try:
                btn.set_active(True)
            finally:
                status_guard["busy"] = False

    for key, label in (("all", "All"), ("available", "Available"), ("installed", "Installed")):
        b = Gtk.ToggleButton(label=label)
        b.set_active(key == "all")
        b.connect("toggled", lambda btn, k=key: on_status_toggle(btn, k))
        status_btns[key] = b
        status_box.append(b)
    filter_row.append(status_box)

    method_drop = Gtk.DropDown.new_from_strings([label for _k, label in METHOD_CHOICES])
    method_drop.add_css_class("fu-source-drop")
    method_drop.set_valign(Gtk.Align.CENTER)
    method_drop.set_tooltip_text("Filter by install method")
    try:
        method_drop.set_enable_search(False)
    except Exception:  # noqa: BLE001
        pass
    filter_row.append(method_drop)

    filter_row.append(search)

    results_lbl = Gtk.Label(label="", xalign=1.0)
    results_lbl.add_css_class("fu-filter-count")
    results_lbl.add_css_class("dim-label")
    results_lbl.set_valign(Gtk.Align.CENTER)
    filter_row.append(results_lbl)

    clear_btn = Gtk.Button(label="Clear filters")
    clear_btn.add_css_class("flat")
    clear_btn.set_valign(Gtk.Align.CENTER)
    clear_btn.set_visible(False)
    clear_btn.set_tooltip_text("Reset search, category, and filters")
    filter_row.append(clear_btn)

    chrome.append(filter_row)
    chrome.append(cat_scroll)
    main.append(chrome)

    list_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    list_host.set_vexpand(True)
    main.append(list_host)

    install_btn_ref: dict[str, Gtk.Button] = {}

    def update_install_btn() -> None:
        btn = install_btn_ref.get("btn")
        if btn is None:
            return
        n = len(selected)
        btn.set_label(f"Install selected ({n})" if n else "Install selected")
        btn.set_sensitive(n > 0)

    def set_selected(app_row: dict[str, str], on: bool) -> None:
        aid = app_row["id"]
        if on:
            selected[aid] = app_row
        else:
            selected.pop(aid, None)
        update_install_btn()

    def row_method_bucket(r: dict[str, str]) -> str:
        label, _css = method_badge_label(r.get("badge", ""), r.get("method", ""))
        if label == "Flatpak":
            return "flatpak"
        if label == "DNF":
            return "dnf"
        return "vendor"

    def filtered_rows() -> list[dict[str, str]]:
        q = filter_query["q"].strip().lower()
        out: list[dict[str, str]] = []
        for r in rows:
            inst = r["installed"] == "1"
            if filter_cat["id"] == "mine":
                if not inst:
                    continue
            elif filter_cat["id"]:
                cid = r["category_id"]
                if cid == "mine":
                    cid = "added"
                if cid != filter_cat["id"]:
                    continue
            if filter_status["v"] == "available" and inst:
                continue
            if filter_status["v"] == "installed" and not inst:
                continue
            if filter_method["v"] != "all" and row_method_bucket(r) != filter_method["v"]:
                continue
            if q:
                blob = f"{r['name']} {r['summary']} {r['package']} {r['category']}".lower()
                if q not in blob:
                    continue
            out.append(r)
        out.sort(key=lambda r: r["name"].lower())
        return out

    def filters_active() -> bool:
        return bool(
            filter_query["q"].strip()
            or filter_status["v"] != "all"
            or filter_method["v"] != "all"
            or filter_cat["id"]
        )

    def sync_filter_chrome(visible_n: int) -> None:
        q = filter_query["q"].strip()
        if q:
            noun = "match" if visible_n == 1 else "matches"
        elif filter_cat["id"] == "cli":
            noun = "tool" if visible_n == 1 else "tools"
        else:
            noun = "app" if visible_n == 1 else "apps"
        results_lbl.set_label(f"{visible_n} {noun}")
        clear_btn.set_visible(filters_active())

    def rebuild_list() -> None:
        while list_host.get_first_child() is not None:
            list_host.remove(list_host.get_first_child())
        visible_checks.clear()

        visible = filtered_rows()
        filtered = filters_active()
        sync_filter_chrome(len(visible))

        grouped: dict[str, list[dict[str, str]]] = {}
        for r in visible:
            grouped.setdefault(r["category"], []).append(r)

        section_order: list[str] = []
        seen_names: set[str] = set()
        for cid in ordered_ids:
            name = cat_names.get(cid)
            if name and name in grouped and name not in seen_names:
                section_order.append(name)
                seen_names.add(name)
        for name in grouped:
            if name not in seen_names:
                section_order.append(name)

        if not grouped:
            scrolled, _clamp, box = page_scroll_body(spacing=14)
            box.set_margin_bottom(8)
            q = filter_query["q"].strip()
            if q:
                empty_title = f"No matches for “{q}”"
                empty_desc = "Try another name, or clear search to browse categories."
            elif filter_cat["id"] == "mine":
                empty_title = "No catalog apps installed"
                empty_desc = (
                    "Install from the catalog and they show up here. "
                    "You can uninstall them from an app’s details."
                )
            elif filter_cat["id"]:
                empty_title = f"Nothing in {cat_full_label(filter_cat['id'])}"
                empty_desc = "Clear filters to see more of the catalog."
            else:
                empty_title = "No matching apps"
                empty_desc = "Clear filters to see more of the catalog."
            empty = Adw.StatusPage(
                title=empty_title,
                description=empty_desc,
                icon_name=(
                    CAT_ICONS.get("mine", "starred-symbolic")
                    if filter_cat["id"] == "mine"
                    else "system-search-symbolic"
                ),
            )
            empty.add_css_class("compact")
            if filter_cat["id"] == "mine" and not q:
                browse = Gtk.Button(label="Browse catalog")
                browse.add_css_class("pill")
                browse.add_css_class("suggested-action")
                browse.set_halign(Gtk.Align.CENTER)
                browse.connect("clicked", lambda *_: clear_filters())
                empty.set_child(browse)
            elif filtered:
                reset = Gtk.Button(label="Clear filters")
                reset.add_css_class("pill")
                reset.add_css_class("suggested-action")
                reset.set_halign(Gtk.Align.CENTER)
                reset.connect("clicked", lambda *_: clear_filters())
                empty.set_child(reset)
            box.append(empty)
            list_host.append(scrolled)
            update_install_btn()
            return

        ordered: list[dict[str, str]] = []
        for cat_name in section_order:
            ordered.extend(sorted(grouped[cat_name], key=lambda r: r["name"].lower()))
        selectable = [r for r in ordered if r.get("installed") != "1"]

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.add_css_class("fu-section-head")
        title = Gtk.Label(label=f"Catalog · {len(ordered)}", xalign=0.0)
        title.add_css_class("fu-section-title")
        title.set_opacity(0.85)
        title.set_hexpand(True)
        head.append(title)
        sel_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        sel_box.set_halign(Gtk.Align.END)
        btn_sel = Gtk.Button(label="Select all")
        btn_sel.add_css_class("flat")
        btn_sel.add_css_class("fu-section-select")
        btn_unsel = Gtk.Button(label="Unselect all")
        btn_unsel.add_css_class("flat")
        btn_unsel.add_css_class("fu-section-select")
        sel_box.append(btn_sel)
        sel_box.append(btn_unsel)
        head.append(sel_box)
        sel_box.set_visible(bool(selectable))
        list_host.append(head)

        store = Gio.ListStore.new(_CatalogItem)
        for row in ordered:
            store.append(_CatalogItem(row))
        bound_checks: dict[str, Gtk.CheckButton] = {}

        def on_setup(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            card.add_css_class("fu-app-mini")
            card.set_hexpand(True)
            card.set_size_request(210, -1)
            try:
                card.set_cursor_from_name("pointer")
            except Exception:  # noqa: BLE001
                pass

            top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            logo = Gtk.Image.new_from_icon_name(_catalog_icon_placeholder())
            logo.set_pixel_size(28)
            logo.set_valign(Gtk.Align.START)
            top.append(logo)

            text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            text.set_hexpand(True)
            name_lab = Gtk.Label(label="", xalign=0.0)
            name_lab.add_css_class("fu-app-mini-title")
            name_lab.set_ellipsize(Pango.EllipsizeMode.END)
            name_lab.set_single_line_mode(True)
            text.append(name_lab)
            sub_lab = Gtk.Label(label="", xalign=0.0)
            sub_lab.add_css_class("fu-app-mini-sub")
            sub_lab.set_ellipsize(Pango.EllipsizeMode.END)
            sub_lab.set_lines(2)
            sub_lab.set_wrap(True)
            sub_lab.set_max_width_chars(28)
            text.append(sub_lab)
            top.append(text)

            trail = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            trail.set_valign(Gtk.Align.START)
            top.append(trail)
            card.append(top)

            foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            meta = Gtk.Label(label="")
            meta.add_css_class("fu-method")
            meta.set_halign(Gtk.Align.START)
            meta.set_hexpand(True)
            foot.append(meta)
            chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
            chev.add_css_class("fu-app-mini-chevron")
            chev.set_pixel_size(12)
            chev.set_valign(Gtk.Align.CENTER)
            chev.set_halign(Gtk.Align.END)
            foot.append(chev)
            card.append(foot)

            cb = Gtk.CheckButton()
            cb.set_valign(Gtk.Align.START)
            cb.set_halign(Gtk.Align.END)
            cb.set_tooltip_text("Select for batch install")
            badge = Gtk.Label(label="Installed")
            badge.add_css_class("fu-badge")
            badge.add_css_class("fu-badge-ok")
            badge.set_valign(Gtk.Align.START)

            card._logo = logo  # type: ignore[attr-defined]
            card._name = name_lab  # type: ignore[attr-defined]
            card._sub = sub_lab  # type: ignore[attr-defined]
            card._trail = trail  # type: ignore[attr-defined]
            card._meta = meta  # type: ignore[attr-defined]
            card._cb = cb  # type: ignore[attr-defined]
            card._badge = badge  # type: ignore[attr-defined]
            card._row = None  # type: ignore[attr-defined]
            card._ignore_toggle = False  # type: ignore[attr-defined]
            card._meta_mod = ""  # type: ignore[attr-defined]

            def on_toggled(button: Gtk.CheckButton) -> None:
                if card._ignore_toggle:  # type: ignore[attr-defined]
                    return
                ar = card._row  # type: ignore[attr-defined]
                if isinstance(ar, dict):
                    set_selected(ar, button.get_active())

            cb.connect("toggled", on_toggled)

            click = Gtk.GestureClick()
            click.set_button(1)

            def on_card_pressed(
                gesture: Gtk.GestureClick,
                n_press: int,
                x: float,
                y: float,
            ) -> None:
                if n_press != 1:
                    return
                ar = card._row  # type: ignore[attr-defined]
                if not isinstance(ar, dict):
                    return
                target = card.pick(x, y, Gtk.PickFlags.DEFAULT)
                wdg = target
                while wdg is not None and wdg is not card:
                    if isinstance(wdg, Gtk.CheckButton):
                        return
                    wdg = wdg.get_parent()
                try:
                    gesture.set_state(Gtk.EventSequenceState.CLAIMED)
                except Exception:  # noqa: BLE001
                    pass
                _show_app_details(card, ar, on_install)

            click.connect("pressed", on_card_pressed)
            card.add_controller(click)
            list_item.set_child(card)

        def on_bind(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
            item = list_item.get_item()
            card = list_item.get_child()
            if not isinstance(item, _CatalogItem) or card is None:
                return
            app_row = item.row
            card._row = app_row  # type: ignore[attr-defined]
            card._name.set_label(app_row.get("name") or "")  # type: ignore[attr-defined]
            summary = (app_row.get("summary") or "").strip()
            card._sub.set_label(summary)  # type: ignore[attr-defined]
            card._sub.set_visible(bool(summary))  # type: ignore[attr-defined]
            card.set_tooltip_text(f"Details for {app_row.get('name') or 'app'}")
            _catalog_set_app_icon(card._logo, app_row, 28)  # type: ignore[attr-defined]

            label, css_mod = method_badge_label(
                app_row.get("badge", ""), app_row.get("method", "")
            )
            meta = card._meta  # type: ignore[attr-defined]
            prev = card._meta_mod  # type: ignore[attr-defined]
            if prev:
                meta.remove_css_class(f"fu-method-{prev}")
            meta.set_label(label)
            meta.add_css_class(f"fu-method-{css_mod}")
            card._meta_mod = css_mod  # type: ignore[attr-defined]

            trail = card._trail  # type: ignore[attr-defined]
            child = trail.get_first_child()
            while child is not None:
                nxt = child.get_next_sibling()
                trail.remove(child)
                child = nxt
            if app_row.get("installed") == "1":
                trail.append(card._badge)  # type: ignore[attr-defined]
            else:
                cb = card._cb  # type: ignore[attr-defined]
                card._ignore_toggle = True  # type: ignore[attr-defined]
                cb.set_active(app_row["id"] in selected)
                card._ignore_toggle = False  # type: ignore[attr-defined]
                trail.append(cb)
                bound_checks[app_row["id"]] = cb

        def on_unbind(_factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem) -> None:
            card = list_item.get_child()
            if card is None:
                return
            ar = card._row  # type: ignore[attr-defined]
            if isinstance(ar, dict):
                bound_checks.pop(ar.get("id") or "", None)
            logo = card._logo  # type: ignore[attr-defined]
            logo._icon_gen = int(getattr(logo, "_icon_gen", 0)) + 1
            logo.set_from_icon_name(_catalog_icon_placeholder())
            card._row = None  # type: ignore[attr-defined]

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", on_setup)
        factory.connect("bind", on_bind)
        factory.connect("unbind", on_unbind)

        grid = Gtk.GridView(model=Gtk.NoSelection(model=store), factory=factory)
        grid.add_css_class("fu-app-grid")
        grid.set_min_columns(1)
        grid.set_max_columns(4)
        grid.set_single_click_activate(False)
        try:
            grid.set_tab_behavior(Gtk.ListTabBehavior.ITEM)
        except (AttributeError, TypeError):
            pass
        grid.set_hexpand(True)
        grid.set_vexpand(True)

        def on_cat_select(on: bool) -> None:
            for ar in selectable:
                set_selected(ar, on)
                cb = bound_checks.get(ar["id"])
                if cb is None:
                    continue
                card = cb.get_parent()
                while card is not None and not hasattr(card, "_ignore_toggle"):
                    card = card.get_parent()
                if card is not None:
                    card._ignore_toggle = True  # type: ignore[attr-defined]
                cb.set_active(on)
                if card is not None:
                    card._ignore_toggle = False  # type: ignore[attr-defined]

        btn_sel.connect("clicked", lambda *_: on_cat_select(True))
        btn_unsel.connect("clicked", lambda *_: on_cat_select(False))

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(grid)
        list_host.append(scrolled)
        update_install_btn()

    def on_search(*_a: object) -> None:
        if search_guard["busy"]:
            return
        filter_query["q"] = search.get_text()
        rebuild_list()

    search.connect("search-changed", on_search)

    def on_method_selected(drop: Gtk.DropDown, *_a: object) -> None:
        if method_guard["busy"]:
            return
        idx = int(drop.get_selected())
        if idx < 0 or idx >= len(METHOD_CHOICES):
            return
        filter_method["v"] = METHOD_CHOICES[idx][0]
        rebuild_list()

    method_drop.connect("notify::selected", on_method_selected)

    def clear_filters(*_a: object) -> None:
        search_guard["busy"] = True
        method_guard["busy"] = True
        try:
            filter_query["q"] = ""
            search.set_text("")
            filter_status["v"] = "all"
            filter_method["v"] = "all"
            filter_cat["id"] = ""
            sync_status_btns()
            sync_cat_btns()
            method_drop.set_selected(0)
        finally:
            search_guard["busy"] = False
            method_guard["busy"] = False
        rebuild_list()

    clear_btn.connect("clicked", clear_filters)

    def on_apps_key(
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        mods = state & Gtk.accelerator_get_default_mod_mask()
        if keyval in {Gdk.KEY_f, Gdk.KEY_F, Gdk.KEY_k, Gdk.KEY_K} and mods == Gdk.ModifierType.CONTROL_MASK:
            search.grab_focus()
            return True
        if keyval == Gdk.KEY_Escape and search.get_text():
            search.set_text("")
            return True
        return False

    keys = Gtk.EventControllerKey()
    keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
    keys.connect("key-pressed", on_apps_key)
    main.add_controller(keys)

    def refresh_after_user_edit() -> None:
        rebuild_cat_rail()
        rebuild_list()

    def show_add_app(*_a: object) -> None:
        existing = {r.get("package", "") for r in rows if r.get("package")}

        def on_added(row: dict[str, str]) -> None:
            if any(r.get("id") == row.get("id") for r in rows):
                return
            rows.append(row)
            filter_cat["id"] = "added"
            refresh_after_user_edit()

        _show_add_user_app_dialog(_widget_window(main), existing, on_added)

    def do_install_selected(*_a: object) -> None:
        if not selected:
            return
        fd, path = tempfile.mkstemp(prefix="urstack-install-", suffix=".txt", text=True)
        os.close(fd)
        batch = Path(path)
        lines = []
        for ar in selected.values():
            lines.append(
                "|".join(
                    [
                        ar.get("method", ""),
                        ar.get("package", ""),
                        ar.get("name", ""),
                        ar.get("url", ""),
                    ]
                )
            )
        batch.write_text("\n".join(lines) + "\n", encoding="utf-8")
        on_install(f"install-batch|{batch}")

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    actions.add_css_class("fu-actions")
    add_btn = mk_btn(
        "Add app",
        "pill fu-secondary",
        pick_icon("list-add-symbolic", "list-add"),
    )
    add_btn.set_hexpand(True)
    add_btn.set_tooltip_text(
        "Add a Flathub ID, DNF package, or Snap name to My apps"
    )
    add_btn.connect("clicked", show_add_app)
    actions.append(add_btn)
    install_btn = mk_btn(
        "Install selected",
        "suggested-action pill fu-primary",
        pick_icon("software-install-symbolic", "folder-download-symbolic", "list-add-symbolic"),
    )
    install_btn.set_hexpand(True)
    install_btn.set_sensitive(False)
    install_btn.connect("clicked", do_install_selected)
    install_btn_ref["btn"] = install_btn
    actions.append(install_btn)
    main.append(pin_page_footer(actions))

    rebuild_list()
    return main




def _load_health_rows(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if not path.is_file():
        return rows
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split("|")
        if len(parts) < 7:
            continue
        rows.append(
            {
                "id": parts[0],
                "section": parts[1],
                "title": parts[2],
                "detail": parts[3],
                "severity": parts[4],
                "actionable": parts[5],
                "selected_default": parts[6],
                "command": parts[7] if len(parts) > 7 else "",
            }
        )
    return rows


STORAGE_FS_IDS = frozenset({"storage-root", "storage-home", "storage-boot"})


def _fmt_bytes(n: int) -> str:
    n = max(0, int(n))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    if i == 0:
        return f"{int(x)} {units[i]}"
    return f"{x:.1f} {units[i]}"


def _load_storage_sidecar(status_file: str) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Parse health.status.storage → (filesystems, hogs sorted by size)."""
    path = Path(f"{status_file}.storage") if status_file else Path()
    filesystems: list[dict[str, str]] = []
    hogs: list[dict[str, str]] = []
    if not path.is_file():
        return filesystems, hogs
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return filesystems, hogs
    for line in raw.splitlines():
        parts = line.split("|")
        if not parts:
            continue
        kind = parts[0]
        if kind == "fs" and len(parts) >= 7:
            filesystems.append(
                {
                    "id": parts[1],
                    "label": parts[2],
                    "path": parts[3],
                    "used": parts[4],
                    "total": parts[5],
                    "pct": parts[6],
                }
            )
        elif kind == "hog" and len(parts) >= 6:
            hogs.append(
                {
                    "id": parts[1],
                    "label": parts[2],
                    "path": parts[3],
                    "bytes": parts[4],
                    "action": parts[5],
                }
            )
    hogs.sort(key=lambda r: int(r.get("bytes") or 0), reverse=True)
    return filesystems, hogs


def _build_storage_optimizer(
    filesystems: list[dict[str, str]],
    hogs: list[dict[str, str]],
) -> Gtk.Widget:
    """Full-width disk meters + largest folders so you can see why the drive is full."""
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    card.add_css_class("fu-overview-card")
    card.add_css_class("fu-storage")
    warn = False
    for fs in filesystems:
        try:
            if int(fs.get("pct") or 0) >= 80:
                warn = True
                break
        except ValueError:
            pass
    if warn:
        card.add_css_class("fu-overview-card-warn")
    else:
        card.add_css_class("fu-overview-card-ok")

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    icon = Gtk.Image.new_from_icon_name(
        pick_icon(
            "drive-partition-symbolic",
            "org.gnome.baobab-symbolic",
            "kr_diskusage-symbolic",
            "disk-quota-symbolic",
            "drive-harddisk-system-symbolic",
        )
    )
    icon.set_pixel_size(34)
    icon.add_css_class("fu-overview-card-icon")
    head.append(icon)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    texts.set_hexpand(True)
    t = Gtk.Label(label="Storage optimiser", xalign=0.0)
    t.add_css_class("fu-overview-card-title")
    texts.append(t)
    s = Gtk.Label(
        label="What’s using space on this machine — pick cleanups below to reclaim it.",
        xalign=0.0,
        wrap=True,
    )
    s.add_css_class("fu-overview-card-status")
    texts.append(s)
    head.append(texts)
    if warn:
        head.append(_health_badge("Low space", "fu-badge-warn"))
    else:
        head.append(_health_badge("OK", "fu-badge-ok"))
    card.append(head)

    for fs in filesystems:
        try:
            used = int(fs.get("used") or 0)
            total = int(fs.get("total") or 0)
            pct = int(fs.get("pct") or 0)
        except ValueError:
            continue
        if total <= 0:
            continue
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        block.add_css_class("fu-storage-meter")
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.add_css_class("fu-storage-meter-top")
        name = Gtk.Label(label=fs.get("label") or fs.get("path") or "/", xalign=0.0)
        name.add_css_class("fu-storage-name")
        name.set_hexpand(True)
        top.append(name)
        free = max(0, total - used)
        meta = Gtk.Label(
            label=f"{pct}% · {_fmt_bytes(free)} free of {_fmt_bytes(total)}",
            xalign=1.0,
        )
        meta.add_css_class("fu-storage-free")
        top.append(meta)
        block.append(top)
        bar = Gtk.ProgressBar()
        bar.set_fraction(min(1.0, used / total))
        bar.set_hexpand(True)
        if pct >= 90:
            bar.add_css_class("fu-storage-bar-crit")
        elif pct >= 80:
            bar.add_css_class("fu-storage-bar-hot")
        block.append(bar)
        card.append(block)

    top_hogs = [h for h in hogs if int(h.get("bytes") or 0) > 0][:8]
    if top_hogs:
        lab = Gtk.Label(label="Largest folders", xalign=0.0)
        lab.add_css_class("fu-section-title")
        lab.set_margin_top(8)
        card.append(lab)
        hog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hog_box.add_css_class("fu-storage-hogs")
        for hog in top_hogs:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("fu-storage-hog")
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            col.set_hexpand(True)
            ht = Gtk.Label(label=hog.get("label") or hog.get("path") or "", xalign=0.0)
            ht.add_css_class("fu-storage-hog-title")
            ht.set_ellipsize(Pango.EllipsizeMode.END)
            col.append(ht)
            path = (hog.get("path") or "").replace(str(Path.home()), "~")
            if path:
                hp = Gtk.Label(label=path, xalign=0.0)
                hp.add_css_class("fu-storage-hog-path")
                hp.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
                col.append(hp)
            row.append(col)
            sz = Gtk.Label(label=_fmt_bytes(int(hog.get("bytes") or 0)), xalign=1.0)
            sz.add_css_class("fu-storage-hog-size")
            row.append(sz)
            hog_box.append(row)
        card.append(hog_box)
    return card


HEALTH_CHOICE_IDS = frozenset(
    {"power-balanced", "power-performance", "power-saver"}
)


def _health_is_choice(row: dict[str, str]) -> bool:
    """True for mutually exclusive switchers (e.g. power profiles), not defects."""
    return row.get("severity") == "choice" or row.get("id") in HEALTH_CHOICE_IDS


def _health_problem_rows(
    rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Return (attention, optional fixes), excluding profile switchers."""
    attention = [r for r in rows if r.get("severity") == "attention"]
    optional = [
        r
        for r in rows
        if r.get("actionable") == "1"
        and r.get("severity") != "attention"
        and not _health_is_choice(r)
    ]
    return attention, optional


def _health_latest_restore_point() -> tuple[str, str]:
    """Return (id, created) for the newest restore point, or ('', '')."""
    root = Path.home() / ".local/state/urstack/health-restore-points"
    if not root.is_dir():
        return "", ""
    ids = sorted((p.name for p in root.iterdir() if p.is_dir()), reverse=True)
    if not ids:
        return "", ""
    rid = ids[0]
    created = ""
    meta = root / rid / "meta.conf"
    if meta.is_file():
        for line in meta.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("created="):
                created = line.split("=", 1)[1].strip()
                break
    return rid, created


def _health_curate_recommendations(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """Pick a short, high-value recommendation list for the hero section."""
    priority = [
        "old-kernels",
        "storage-root",
        "storage-home",
        "storage-boot",
        "trash",
        "dnf-autoremove",
        "flatpak-orphans",
        "thumbnails",
        "coredumps",
        "flathub",
        "rpmfusion",
        "codecs",
        "earlyoom",
        "zram",
        "power-ppd",
        "journal-vacuum",
        "dnf-cache",
        "flatpak-unused",
        "pip-cache",
        "npm-cache",
        "cargo-cache",
        "podman-prune",
        "docker-prune",
        "snap-old",
        "tlp-conflict",
        "dnf-speed",
        "sysctl",
        "fstrim",
    ]
    rank = {k: i for i, k in enumerate(priority)}
    actionable = [r for r in rows if r.get("actionable") == "1"]
    # Prefer attention, then curated priority; skip power profile toggles from recs
    skip = HEALTH_CHOICE_IDS
    actionable = [
        r
        for r in actionable
        if r["id"] not in skip and r.get("severity") != "choice"
    ]

    def sort_key(r: dict[str, str]) -> tuple[int, int, str]:
        sev = 0 if r.get("severity") == "attention" else 1
        return (sev, rank.get(r["id"], 100), r.get("title", "").lower())

    actionable.sort(key=sort_key)
    return actionable[:6]


def build_health_content(
    status_file: str,
    *,
    parent_win: Gtk.Window | None = None,
    on_apply: Callable[[list[str], bool], None],
    on_refresh: Callable[[], None] | None = None,
    on_create_restore_point: Callable[[], None] | None = None,
    on_restore_latest: Callable[[], None] | None = None,
    scanning: bool = False,
) -> Gtk.Widget:
    """Curated System Health: recommendations, restore points, full checks."""
    SECTION_ORDER = [
        ("storage", "Storage"),
        ("cleanup", "Cleanup"),
        ("workstation", "Workstation base"),
        ("memory", "Memory & boot"),
        ("power", "Power"),
        ("advanced", "Advanced"),
        ("info", "Notes"),
    ]
    SECTION_META = {
        "storage": (
            pick_icon(
                "drive-partition-symbolic",
                "org.gnome.baobab-symbolic",
                "kr_diskusage-symbolic",
                "disk-quota-symbolic",
                "drive-harddisk-system-symbolic",
            ),
            "Disk use, what’s filling the drive, and cleanup tools.",
        ),
        "cleanup": (
            "user-trash-symbolic",
            "Kernels, caches, journals, leftover Flatpak data.",
        ),
        "workstation": (
            "computer-symbolic",
            "Flathub, RPM Fusion, codecs, and firmware.",
        ),
        "memory": (
            pick_icon(
                "org.gnome.SystemMonitor-symbolic",
                "speedometer-symbolic",
                "system-reboot-symbolic",
                "chronometer-symbolic",
                "applications-system-symbolic",
            ),
            "zram, EarlyOOM, and boot timing.",
        ),
        "power": (
            pick_icon(
                "system-shutdown-symbolic",
                "battery-symbolic",
                "battery-good-symbolic",
            ),
            "Power profiles and TLP conflicts.",
        ),
        "advanced": (
            pick_icon("preferences-other-symbolic", "applications-engineering-symbolic"),
            "Sysctl, SSD trim, DNF tuning, optional user units.",
        ),
        "info": (
            pick_icon("info-symbolic", "dialog-information-symbolic"),
            "Read-only notes from this scan.",
        ),
    }
    SEV_LABEL = {
        "ok": ("OK", "fu-badge-ok"),
        "attention": ("Attention", "fu-badge-warn"),
        "available": ("Suggested", "fu-badge-warn"),
        "choice": ("Switch", "fu-badge"),
        "info": ("Info", "fu-badge"),
    }
    DESTRUCTIVE = {
        "old-kernels",
        "flatpak-orphans",
        "dnf-autoremove",
        "podman-prune",
        "docker-prune",
        "snap-old",
    }
    WHY = {
        "old-kernels": "Frees /boot space and keeps only recent kernels.",
        "dnf-cache": "Reclaims package download cache without removing apps.",
        "journal-vacuum": "Caps systemd journal growth on disk.",
        "flatpak-unused": "Drops leftover runtimes nothing needs anymore.",
        "flatpak-orphans": "Removes data dirs for apps you already uninstalled.",
        "trash": "Permanently deletes files already in the desktop trash.",
        "thumbnails": "Regenerates as you browse pictures; safe to clear.",
        "pip-cache": "Downloaded Python wheels. pip will fetch them again if needed.",
        "npm-cache": "npm’s download cache. Packages you installed stay installed.",
        "cargo-cache": "Cached crate downloads only — not binaries in ~/.cargo/bin.",
        "dnf-autoremove": "Packages DNF installed as deps that nothing needs now.",
        "podman-prune": "Removes unused Podman images, containers, and networks.",
        "docker-prune": "Removes unused Docker images, containers, and networks.",
        "coredumps": "Crash dumps under /var/lib/systemd/coredump.",
        "snap-old": "Disabled older Snap revisions kept after refreshes.",
        "flathub": "Unlocks the main Flatpak app store for this user.",
        "rpmfusion": "Enables the usual Fedora multimedia/driver repos.",
        "codecs": "Installs the common playback codec set.",
        "zram": "Compressed RAM swap — better than a cold disk swap for desktops.",
        "earlyoom": "Kills the worst offender under memory pressure before the machine freezes.",
        "power-ppd": (
            "Enables power profiles (tuned-ppd on KDE, power-profiles-daemon on GNOME) "
            "so you can switch balanced / performance / power-saver."
        ),
        "tlp-conflict": "TLP and a power-profiles service fighting causes odd power behaviour.",
        "fstrim": "One-shot SSD trim; safe on modern NVMe/SATA SSDs.",
        "dnf-speed": "Faster mirror selection and parallel downloads for DNF.",
        "sysctl": "Sensible swappiness + higher inotify limits for IDEs and file watchers.",
    }

    root = page_frame()

    refresh_btn = mk_btn("Refresh", "flat", "view-refresh-symbolic")
    refresh_btn.set_valign(Gtk.Align.CENTER)
    if on_refresh:
        refresh_btn.connect("clicked", lambda *_: on_refresh())
    else:
        refresh_btn.set_sensitive(False)

    hero_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    hero_host.set_hexpand(True)
    root.append(hero_host)

    list_host = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    list_host.set_vexpand(True)
    root.append(list_host)

    selected: dict[str, dict[str, str]] = {}
    visible_checks: list[tuple[dict[str, str], Gtk.CheckButton]] = []
    apply_btn_ref: dict[str, Gtk.Button | None] = {"btn": None}
    make_rp = {"v": True}  # create restore point before apply (default on)

    def update_apply_btn() -> None:
        btn = apply_btn_ref["btn"]
        if btn is None:
            return
        n = len(selected)
        btn.set_sensitive(n > 0)
        btn.set_label(
            f"Apply recommendations ({n})" if n else "Apply recommendations"
        )

    def set_selected(row: dict[str, str], on: bool) -> None:
        rid = row["id"]
        if on:
            selected[rid] = row
        else:
            selected.pop(rid, None)
        update_apply_btn()

    def rebuild_list() -> None:
        while hero_host.get_first_child() is not None:
            hero_host.remove(hero_host.get_first_child())
        while list_host.get_first_child() is not None:
            list_host.remove(list_host.get_first_child())
        visible_checks.clear()
        selected.clear()

        scrolled, _clamp, box = page_scroll_body(spacing=14, side_pad=0)
        box.set_margin_bottom(8)
        refresh_btn.set_sensitive(not scanning)

        rows = [] if scanning and not Path(status_file).is_file() else _load_health_rows(Path(status_file))
        recs = _health_curate_recommendations(rows)
        attention, optional = _health_problem_rows(rows)
        attention_n = len(attention)
        actionable_n = len(optional)
        ok_n = sum(1 for r in rows if r.get("severity") == "ok")

        # ── Hero ──────────────────────────────────────────────────────────
        if scanning and not rows:
            score_txt, score_sub = "…", "Scanning"
            hero_title, hero_sub = "Checking this workstation", "Kernels, caches, Flatpak, power, and tuneables."
            hero_warn = False
        elif not rows:
            score_txt, score_sub = "—", "Run a scan"
            hero_title, hero_sub = "No scan yet", "Refresh to analyse this workstation."
            hero_warn = False
        elif scanning:
            score_txt, score_sub = "…", "Scanning"
            hero_title, hero_sub = "Checking this workstation", "Results update when the scan finishes."
            hero_warn = False
        elif actionable_n == 0:
            score_txt, score_sub = "100", "Health score"
            hero_title, hero_sub = "Looking sharp", f"{ok_n} checks clear · nothing urgent."
            hero_warn = False
        else:
            score_val = max(55, 100 - attention_n * 12 - max(0, actionable_n - attention_n) * 5)
            score_txt, score_sub = str(score_val), "Health score"
            hero_title = f"{len(recs)} recommendation{'s' if len(recs) != 1 else ''}"
            hero_sub = f"{attention_n} need attention · {actionable_n} optional fixes"
            hero_warn = True
        prev = refresh_btn.get_parent()
        if prev is not None:
            prev.remove(refresh_btn)
        hero_host.append(
            page_hero(
                score_txt,
                score_sub,
                hero_title,
                hero_sub,
                warn=hero_warn,
                ok=bool(rows) and not scanning and not hero_warn,
                heading="System Health",
                heading_sub="Curated fixes for this Fedora workstation — with a restore point safety net.",
                icon_name=page_icon("health"),
                heading_trailing=refresh_btn,
            )
        )

        # ── Restore point strip ───────────────────────────────────────────
        rp_id, rp_created = _health_latest_restore_point()
        rp_btns: list[Gtk.Widget] = []
        if on_create_restore_point:
            b_new = Gtk.Button(label="Create now")
            b_new.add_css_class("flat")
            b_new.connect("clicked", lambda *_: on_create_restore_point())
            rp_btns.append(b_new)
        if on_restore_latest:
            b_rest = Gtk.Button(label="Restore latest")
            b_rest.add_css_class("destructive-action")
            b_rest.set_sensitive(bool(rp_id))
            b_rest.connect("clicked", lambda *_: on_restore_latest())
            rp_btns.append(b_rest)
        if rp_id:
            pretty = rp_created or rp_id
            rp_sub = f"Latest: {pretty} — roll back Health changes if something breaks."
        else:
            rp_sub = "None yet. One is created automatically before you apply fixes."
        box.append(page_callout("Restore point", rp_sub, *rp_btns))

        if not rows:
            empty = Adw.StatusPage(
                title="Scanning…" if scanning else "No health data",
                description=(
                    "Checking kernels, caches, Flatpak, power, and tuneables."
                    if scanning
                    else "Tap Refresh to scan this workstation."
                ),
                icon_name=page_icon("health"),
            )
            empty.add_css_class("compact")
            box.append(empty)
        else:
            # ── Recommended for you ───────────────────────────────────────
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            head.add_css_class("fu-section-head")
            ht2 = Gtk.Label(label="Recommended for you", xalign=0.0)
            ht2.add_css_class("fu-section-title")
            ht2.set_hexpand(True)
            head.append(ht2)
            if recs:
                sel_all = Gtk.Button(label="Select all")
                sel_all.add_css_class("flat")
                sel_all.add_css_class("fu-section-select")
                unsel = Gtk.Button(label="Unselect all")
                unsel.add_css_class("flat")
                unsel.add_css_class("fu-section-select")
                rec_checks: list[tuple[dict[str, str], Gtk.CheckButton]] = []

                def _sel(on: bool, checks: list = rec_checks) -> None:
                    for _a, cb in checks:
                        cb.set_active(on)

                sel_all.connect("clicked", lambda *_: _sel(True))
                unsel.connect("clicked", lambda *_: _sel(False))
                head.append(sel_all)
                head.append(unsel)
            box.append(head)

            if not recs:
                good = Gtk.Label(
                    label="Nothing urgent — you’re in good shape. Browse all checks below if you want.",
                    xalign=0.0,
                    wrap=True,
                )
                good.add_css_class("dim-label")
                box.append(good)
            else:
                rec_cards: list[Gtk.Widget] = []
                for app_row in recs:
                    rec_cards.append(
                        _health_rec_card(
                            app_row,
                            SEV_LABEL,
                            WHY,
                            set_selected,
                            visible_checks,
                            rec_checks,
                        )
                    )
                box.append(_health_card_grid(rec_cards, 3))

            # ── All checks (info cards) ───────────────────────────────────
            by_sec: dict[str, list[dict[str, str]]] = {}
            for r in rows:
                by_sec.setdefault(r["section"], []).append(r)
            rec_ids = {r["id"] for r in recs}

            box.append(page_section_label("All checks"))

            sec_cards: list[Gtk.Widget] = []
            for sec_id, sec_title in SECTION_ORDER:
                apps = by_sec.get(sec_id) or []
                if sec_id == "storage":
                    apps = [r for r in apps if r.get("id") not in STORAGE_FS_IDS]
                if not apps:
                    continue
                icon, blurb = SECTION_META.get(
                    sec_id, (page_icon("health"), "")
                )
                sec_cards.append(
                    _health_section_card(
                        sec_title,
                        icon,
                        blurb,
                        apps,
                        SEV_LABEL,
                        selected,
                        set_selected,
                        visible_checks,
                        rec_ids,
                        parent_win=parent_win,
                        status_file=status_file,
                    )
                )
            box.append(_health_card_grid(sec_cards, 3))

        storage_fs, storage_hogs = _load_storage_sidecar(status_file)
        if storage_fs or storage_hogs:
            box.append(_build_storage_optimizer(storage_fs, storage_hogs))

        list_host.append(scrolled)
        update_apply_btn()

    def do_apply(*_a: object) -> None:
        if not selected:
            return
        ids = list(selected.keys())
        destructive = [selected[i] for i in ids if i in DESTRUCTIVE]
        note = ""
        if make_rp["v"]:
            note = (
                "\n\nA restore point will be created first. It can undo UrStack's "
                "config changes and service enablement, plus the DNF transaction for "
                "package actions. Deleted caches, trash and logs cannot be recovered."
            )
        if destructive and parent_win is not None:
            body_lines = [f"• {r['title']}: {r['detail']}" for r in destructive]
            body = (
                "These actions remove data or packages:\n\n"
                + "\n".join(body_lines)
                + note
                + "\n\nContinue?"
            )
            try:
                dialog = Adw.AlertDialog(
                    heading="Confirm health changes",
                    body=body,
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("apply", "Apply")
                dialog.set_response_appearance(
                    "apply", Adw.ResponseAppearance.DESTRUCTIVE
                )
                dialog.set_default_response("cancel")
                dialog.set_close_response("cancel")

                def on_resp(_d: Adw.AlertDialog, response: str) -> None:
                    if response == "apply":
                        on_apply(ids, make_rp["v"])

                dialog.connect("response", on_resp)
                dialog.present(parent_win)
                return
            except Exception:  # noqa: BLE001
                pass
        on_apply(ids, make_rp["v"])

    # Footer: restore-point toggle + apply
    footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    footer.add_css_class("fu-actions")
    rp_toggle = Gtk.CheckButton(label="Create restore point before applying")
    rp_toggle.set_active(True)

    def on_rp_toggle(btn: Gtk.CheckButton) -> None:
        make_rp["v"] = btn.get_active()

    rp_toggle.connect("toggled", on_rp_toggle)
    footer.append(rp_toggle)
    apply_btn = mk_btn(
        "Apply recommendations",
        "suggested-action pill fu-primary",
        "emblem-ok-symbolic",
    )
    apply_btn.set_hexpand(True)
    apply_btn.set_sensitive(False)
    apply_btn.connect("clicked", do_apply)
    apply_btn_ref["btn"] = apply_btn
    footer.append(apply_btn)
    root.append(pin_page_footer(footer))

    rebuild_list()
    return root


def _health_card_grid(cards: list[Gtk.Widget], columns: int = 3) -> Gtk.Widget:
    """Fixed N-column grid so health info cards don't collapse to one column."""
    return page_card_grid(cards, columns)


def _health_badge(label: str, css: str) -> Gtk.Label:
    badge = Gtk.Label(label=label)
    badge.add_css_class("fu-badge")
    badge.add_css_class(css)
    badge.set_valign(Gtk.Align.START)
    badge.set_margin_top(2)
    return badge


def _health_detail_body(app_row: dict[str, str], status_file: str) -> str:
    rid = app_row.get("id") or ""
    if rid == "boot-blame" and status_file:
        path = Path(f"{status_file}.boot-blame")
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    return text
            except OSError:
                pass
    return (app_row.get("detail") or "").strip()


def _show_health_detail(
    parent_win: Gtk.Window | None,
    title: str,
    body: str,
) -> None:
    if not body:
        return
    try:
        dialog = Adw.AlertDialog(heading=title, body="")
        dialog.add_response("ok", "Close")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_min_content_height(320)
        scrolled.set_min_content_width(480)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        lab = Gtk.Label(label=body, xalign=0.0, yalign=0.0)
        lab.set_selectable(True)
        lab.set_wrap(True)
        lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lab.add_css_class("fu-mono")
        lab.set_margin_start(6)
        lab.set_margin_end(6)
        lab.set_margin_top(4)
        lab.set_margin_bottom(4)
        scrolled.set_child(lab)
        dialog.set_extra_child(scrolled)
        dialog.present(parent_win)
    except Exception:  # noqa: BLE001
        try:
            dialog = Adw.AlertDialog(heading=title, body=body[:4000])
            dialog.add_response("ok", "Close")
            dialog.set_default_response("ok")
            dialog.present(parent_win)
        except Exception:  # noqa: BLE001
            pass


def _health_item_row(
    app_row: dict[str, str],
    sev_label: dict[str, tuple[str, str]],
    selected: dict[str, dict[str, str]],
    set_selected: Callable[[dict[str, str], bool], None],
    visible_checks: list[tuple[dict[str, str], Gtk.CheckButton]],
    section_checks: list[tuple[dict[str, str], Gtk.CheckButton]],
    *,
    highlight: bool = False,
    parent_win: Gtk.Window | None = None,
    status_file: str = "",
) -> Gtk.Widget:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.add_css_class("fu-health-item")
    row.set_hexpand(True)

    if app_row.get("actionable") == "1":
        cb = Gtk.CheckButton()
        cb.set_valign(Gtk.Align.START)
        cb.set_margin_top(1)

        def on_toggled(
            button: Gtk.CheckButton,
            ar: dict[str, str] = app_row,
        ) -> None:
            set_selected(ar, button.get_active())

        cb.connect("toggled", on_toggled)
        if app_row["id"] in selected:
            cb.set_active(True)
        row.append(cb)
        visible_checks.append((app_row, cb))
        section_checks.append((app_row, cb))

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    texts.set_hexpand(True)
    title = app_row["title"]
    if highlight:
        title = f"★ {title}"
    t = Gtk.Label(label=title, xalign=0.0, wrap=True)
    t.add_css_class("fu-health-item-title")
    t.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    t.set_hexpand(True)
    t.set_lines(2)
    t.set_ellipsize(Pango.EllipsizeMode.END)
    texts.append(t)
    detail = (app_row.get("detail") or "").strip()
    if detail:
        d = Gtk.Label(label=detail, xalign=0.0, wrap=True)
        d.add_css_class("fu-health-item-sub")
        d.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        d.set_hexpand(True)
        d.set_lines(2)
        d.set_ellipsize(Pango.EllipsizeMode.END)
        texts.append(d)
    row.append(texts)

    sev = app_row.get("severity") or "info"
    if _health_is_choice(app_row) and app_row.get("actionable") != "1":
        lab, css = "Current", "fu-badge-ok"
    elif _health_is_choice(app_row):
        lab, css = "Switch", "fu-badge"
    else:
        lab, css = sev_label.get(sev, ("Info", "fu-badge"))
    row.append(_health_badge(lab, css))
    full = _health_detail_body(app_row, status_file)
    clickable = app_row.get("actionable") != "1" and (
        app_row.get("id") == "boot-blame" or len(full) > 90
    )
    if clickable:
        chev = Gtk.Image.new_from_icon_name("go-next-symbolic")
        chev.set_pixel_size(12)
        chev.set_valign(Gtk.Align.CENTER)
        chev.set_opacity(0.55)
        row.append(chev)
        btn = Gtk.Button()
        btn.set_has_frame(False)
        btn.add_css_class("flat")
        btn.add_css_class("fu-health-item-btn")
        btn.set_hexpand(True)
        btn.set_child(row)
        btn.set_tooltip_text("Show full details")
        btn.connect(
            "clicked",
            lambda *_a, ar=app_row, text=full: _show_health_detail(
                parent_win, ar.get("title") or "Details", text
            ),
        )
        return btn
    return row


def _health_rec_card(
    app_row: dict[str, str],
    sev_label: dict[str, tuple[str, str]],
    why_map: dict[str, str],
    set_selected: Callable[[dict[str, str], bool], None],
    visible_checks: list[tuple[dict[str, str], Gtk.CheckButton]],
    rec_checks: list[tuple[dict[str, str], Gtk.CheckButton]],
) -> Gtk.Widget:
    sev = app_row.get("severity") or "available"
    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    card.add_css_class("fu-overview-card")
    if sev == "attention":
        card.add_css_class("fu-overview-card-warn")
    card.set_hexpand(True)
    card.set_vexpand(True)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    titles.set_hexpand(True)
    t = Gtk.Label(label=app_row["title"], xalign=0.0)
    t.add_css_class("fu-overview-card-title")
    t.set_wrap(True)
    t.set_lines(2)
    t.set_ellipsize(Pango.EllipsizeMode.END)
    titles.append(t)
    why = why_map.get(app_row["id"], app_row.get("detail") or "")
    if why:
        s = Gtk.Label(label=why, xalign=0.0, wrap=True)
        s.add_css_class("fu-overview-card-status")
        s.set_lines(3)
        s.set_ellipsize(Pango.EllipsizeMode.END)
        titles.append(s)
    head.append(titles)
    lab, css = sev_label.get(sev, ("Suggested", "fu-badge-warn"))
    head.append(_health_badge(lab, css))
    card.append(head)

    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    body.set_vexpand(True)
    cmd = (app_row.get("command") or "").strip()
    if cmd:
        c = Gtk.Label(label=f"$ {cmd}", xalign=0.0, wrap=True)
        c.add_css_class("fu-health-rec-cmd")
        c.set_margin_top(8)
        body.append(c)
    card.append(body)

    foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    foot.set_margin_top(12)
    cb = Gtk.CheckButton(label="Include in apply")
    pre = sev == "attention" or app_row.get("selected_default") == "1"
    cb.set_active(pre)

    def on_toggled(
        button: Gtk.CheckButton,
        ar: dict[str, str] = app_row,
    ) -> None:
        set_selected(ar, button.get_active())

    cb.connect("toggled", on_toggled)
    if pre:
        set_selected(app_row, True)
    foot.append(cb)
    card.append(foot)
    visible_checks.append((app_row, cb))
    rec_checks.append((app_row, cb))
    return card


def _health_section_card(
    title: str,
    icon_name: str,
    blurb: str,
    apps: list[dict[str, str]],
    sev_label: dict[str, tuple[str, str]],
    selected: dict[str, dict[str, str]],
    set_selected: Callable[[dict[str, str], bool], None],
    visible_checks: list[tuple[dict[str, str], Gtk.CheckButton]],
    rec_ids: set[str],
    *,
    parent_win: Gtk.Window | None = None,
    status_file: str = "",
) -> Gtk.Widget:
    attention_n = sum(1 for r in apps if r.get("severity") == "attention")
    actionable = [
        r
        for r in apps
        if r.get("actionable") == "1" and not _health_is_choice(r)
    ]
    ok_n = sum(1 for r in apps if r.get("severity") == "ok")

    if attention_n:
        status = f"{attention_n} need attention · {len(apps)} checks"
        badge, badge_css = "Attention", "fu-badge-warn"
        warn, ok = True, False
    elif actionable:
        status = f"{len(actionable)} optional · {len(apps)} checks"
        badge, badge_css = "Suggested", "fu-badge-warn"
        warn, ok = True, False
    else:
        status = f"{ok_n} clear · {len(apps)} checks"
        badge, badge_css = "OK", "fu-badge-ok"
        warn, ok = False, True

    card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    card.add_css_class("fu-overview-card")
    if warn:
        card.add_css_class("fu-overview-card-warn")
    elif ok:
        card.add_css_class("fu-overview-card-ok")
    card.set_hexpand(True)
    card.set_vexpand(True)

    head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(34)
    icon.add_css_class("fu-overview-card-icon")
    icon.set_valign(Gtk.Align.START)
    head.append(icon)
    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    titles.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0.0)
    t.add_css_class("fu-overview-card-title")
    t.set_ellipsize(Pango.EllipsizeMode.END)
    t.set_single_line_mode(True)
    titles.append(t)
    s = Gtk.Label(label=status, xalign=0.0, wrap=True)
    s.add_css_class("fu-overview-card-status")
    titles.append(s)
    head.append(titles)
    head.append(_health_badge(badge, badge_css))
    card.append(head)

    if blurb:
        b = Gtk.Label(label=blurb, xalign=0.0, wrap=True)
        b.add_css_class("fu-overview-card-blurb")
        b.set_lines(2)
        b.set_ellipsize(Pango.EllipsizeMode.END)
        card.append(b)

    items = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    items.add_css_class("fu-health-items")
    items.set_vexpand(True)
    section_checks: list[tuple[dict[str, str], Gtk.CheckButton]] = []
    for app_row in apps:
        items.append(
            _health_item_row(
                app_row,
                sev_label,
                selected,
                set_selected,
                visible_checks,
                section_checks,
                highlight=app_row["id"] in rec_ids,
                parent_win=parent_win,
                status_file=status_file,
            )
        )
    card.append(items)

    if section_checks:
        foot = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        foot.set_margin_top(10)
        sel_all = Gtk.Button(label="Select all")
        sel_all.add_css_class("flat")
        sel_all.add_css_class("fu-section-select")
        unsel = Gtk.Button(label="Unselect all")
        unsel.add_css_class("flat")
        unsel.add_css_class("fu-section-select")

        def _sel(on: bool, checks: list = section_checks) -> None:
            for _a, cb in checks:
                cb.set_active(on)

        sel_all.connect("clicked", lambda *_: _sel(True))
        unsel.connect("clicked", lambda *_: _sel(False))
        foot.append(sel_all)
        foot.append(unsel)
        card.append(foot)
    return card


BACKUP_INCLUDE_OPTIONS: list[tuple[str, str, str, bool]] = [
    # key, title, subtitle, default_on
    (
        "manifests",
        "Package & CLI manifests",
        "DNF / Flatpak / Snap lists, hardware inventory, npm / pip / cargo / PATH tools.",
        True,
    ),
    (
        "appimages",
        "AppImages & vendor apps",
        "Copy AppImages from ~/Applications and note non-store launchers.",
        True,
    ),
    (
        "projects",
        "Project repositories",
        "Git trees under your configured project folders, plus any custom paths you add below.",
        True,
    ),
    (
        "settings",
        "Desktop & app settings",
        "Themes, Plasma/GNOME prefs, Cursor settings, desktop files, overlays. "
        "For a portable wallpaper + icons + widgets pack, use the Look page.",
        True,
    ),
    (
        "secrets",
        "Secrets & identity",
        "SSH keys, GPG, git credentials, GitHub CLI config, KDE Wallet. "
        "Sensitive — off by default; keep the backup private if you turn it on.",
        False,
    ),
    (
        "browsers",
        "Browser bookmarks",
        "Chrome bookmarks / PWAs and Firefox places database.",
        True,
    ),
    (
        "full_dotconfig",
        "Full ~/.config & ~/.local/share",
        "Broader rsync of config trees (caches excluded). Larger backup, closer rebuild.",
        True,
    ),
    (
        "system",
        "System snippets",
        "GRUB, SDDM, sysctl, CUPS printers, crontab, snap user data.",
        True,
    ),
]

RESTORE_INCLUDE_OPTIONS: list[tuple[str, str, str, bool]] = [
    (
        "packages",
        "DNF repos & packages",
        "Restore yum repos and reinstall user-installed RPM packages.",
        True,
    ),
    (
        "drivers",
        "Drivers",
        "Choose and install GPU / driver groups (hardware prompt).",
        True,
    ),
    (
        "flatpak",
        "Flatpak apps",
        "Reinstall Flatpak applications from the backup list.",
        True,
    ),
    (
        "snap",
        "Snap packages & data",
        "Reinstall snaps and restore ~/snap user data when present.",
        True,
    ),
    (
        "projects",
        "Project trees",
        "Copy archived git projects and custom paths back under your home directory.",
        True,
    ),
    (
        "settings",
        "Home settings overlay",
        "Restore themes, desktop prefs, and other home overlay files.",
        True,
    ),
    (
        "secrets",
        "Secrets & identity",
        "Restore SSH, GPG, and credential files from the overlay. Off by default.",
        False,
    ),
    (
        "browsers",
        "Browser bookmarks",
        "Restore Firefox bookmark databases when a profile exists.",
        True,
    ),
    (
        "appimages",
        "AppImages & vendor notes",
        "Copy AppImages back to ~/Applications.",
        True,
    ),
    (
        "programs",
        "Programs & CLIs",
        "nvm / npm, rustup / cargo, pipx, Cursor extensions, and similar tools.",
        True,
    ),
    (
        "system",
        "System config",
        "GRUB, SDDM, locale, printers, groups, crontab, bin scripts.",
        True,
    ),
]

# Quick include combinations shown above the per-section switches. Secrets are
# left off by every preset except the explicitly labelled one, so no single click
# can quietly sweep SSH keys, GPG material and wallets into a backup.
BACKUP_PRESETS: list[tuple[str, str, dict[str, bool]]] = [
    (
        "this",
        "This computer",
        {key: key != "secrets" for key, *_rest in BACKUP_INCLUDE_OPTIONS},
    ),
    (
        "packages",
        "Packages only",
        {key: key == "manifests" for key, *_rest in BACKUP_INCLUDE_OPTIONS},
    ),
    (
        "everything",
        "Everything + secrets",
        {key: True for key, *_rest in BACKUP_INCLUDE_OPTIONS},
    ),
]
RESTORE_PRESETS: list[tuple[str, str, dict[str, bool]]] = [
    (
        "this",
        "This computer",
        {key: key != "secrets" for key, *_rest in RESTORE_INCLUDE_OPTIONS},
    ),
    (
        "new",
        "New computer",
        {
            key: key not in {"drivers", "system", "secrets"}
            for key, *_rest in RESTORE_INCLUDE_OPTIONS
        },
    ),
    (
        "packages",
        "Packages only",
        {
            key: key in {"packages", "flatpak", "snap", "programs"}
            for key, *_rest in RESTORE_INCLUDE_OPTIONS
        },
    ),
    (
        "everything",
        "Everything + secrets",
        {key: True for key, *_rest in RESTORE_INCLUDE_OPTIONS},
    ),
]
DESKTOP_PRESETS: list[tuple[str, str]] = [
    ("all", "All desktops"),
    ("kde", "KDE"),
    ("gnome", "GNOME"),
]


def _backup_opts_path(mode: str) -> Path:
    return Path.home() / ".config" / "urstack" / f"last-{mode}-opts.conf"


def _extra_paths_file() -> Path:
    return Path.home() / ".config" / "urstack" / "backup-extra-paths.conf"


def _load_extra_paths() -> list[str]:
    path = _extra_paths_file()
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def _save_extra_paths(paths: list[str]) -> None:
    path = _extra_paths_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # De-dupe while preserving order
    seen: set[str] = set()
    clean: list[str] = []
    for p in paths:
        p = p.strip()
        if not p or p in seen:
            continue
        seen.add(p)
        clean.append(p)
    body = "# Extra folders/files included in UrStack backups\n"
    body += "\n".join(clean) + ("\n" if clean else "")
    path.write_text(body, encoding="utf-8")


def _load_include_defaults(
    mode: str, options: list[tuple[str, str, str, bool]]
) -> dict[str, bool]:
    defaults = {key: default for key, _t, _s, default in options}
    path = _backup_opts_path(mode)
    if not path.is_file():
        # Prefer config full_dotconfig for backup when no saved prefs
        if mode == "backup":
            cfg = read_config_map(Path.home() / ".config" / "urstack" / "config.conf")
            if "backup_full_dotconfig" in cfg:
                defaults["full_dotconfig"] = cfg.get("backup_full_dotconfig", "1") == "1"
        return defaults
    saved = read_config_map(path)
    for key in defaults:
        if key in saved:
            defaults[key] = saved[key] == "1"
    return defaults


def _save_include_defaults(
    mode: str, values: dict[str, bool], extra: dict[str, str] | None = None
) -> None:
    path = _backup_opts_path(mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={'1' if v else '0'}" for k, v in values.items()]
    if extra:
        lines.extend(f"{k}={v}" for k, v in extra.items())
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _encode_include_opts(values: dict[str, bool]) -> str:
    return ",".join(f"{k}={'1' if v else '0'}" for k, v in values.items())


def build_backup_restore_content(
    mode: str,
    *,
    parent_win: Gtk.Window,
    on_start: Callable[[str], None],
    on_back: Callable[[], None] | None = None,
) -> Gtk.Widget:
    """In-app Backup / Restore page with per-section include controls."""
    from datetime import date

    is_backup = mode == "backup"
    chosen = {"path": ""}
    option_defs = BACKUP_INCLUDE_OPTIONS if is_backup else RESTORE_INCLUDE_OPTIONS
    defaults = _load_include_defaults(mode, option_defs)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_hexpand(True)
    outer.set_vexpand(True)

    scrolled, clamp, col = page_scroll_body(spacing=14)

    chrome = page_chrome_box()
    chrome.append(
        page_hero(
            "1" if is_backup else "↺",
            "archive" if is_backup else "restore",
            "Backup this workstation" if is_backup else "Restore a workstation",
            (
                "Choose a destination and what to include. Setup rebuild archive — not a full disk clone."
                if is_backup
                else "Pick a backup folder and choose what to bring back. Matching files may be overwritten."
            ),
            warn=False,
            heading="Backup" if is_backup else "Restore",
            heading_sub=(
                "Capture this workstation so you can rebuild it later."
                if is_backup
                else "Rebuild this machine from a fedora-setup-* archive."
            ),
            icon_name=page_icon("backup") if is_backup else page_icon("restore"),
        )
    )
    outer.append(chrome)
    col.append(
        page_callout(
            "Safety",
            (
                "Manifests and settings are cheap to keep. Secrets (SSH/GPG) are "
                "opt-in — only enable if you trust the destination."
                if is_backup
                else "Review each section before starting. Restore does not wipe the disk; it overlays selected pieces."
            ),
        )
    )

    col.append(page_section_label("Location"))
    path_group = Adw.PreferencesGroup()
    path_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    path_wrap.add_css_class("fu-page-body")
    path_row = Adw.ActionRow(
        title="No folder selected",
        subtitle="Choose where to write the backup" if is_backup else "Select a fedora-setup-* folder",
    )
    try:
        path_row.set_subtitle_lines(3)
    except AttributeError:
        pass
    pick_btn = mk_btn("Choose folder", "fu-row-suffix", "folder-symbolic")
    pick_btn.set_valign(Gtk.Align.CENTER)
    try:
        pick_btn.set_can_shrink(False)
    except Exception:  # noqa: BLE001
        pass
    path_row.add_suffix(pick_btn)
    path_group.add(path_row)
    preview_lbl = Gtk.Label(label="", xalign=0.0, wrap=True)
    preview_lbl.add_css_class("dim-label")
    preview_lbl.add_css_class("caption")
    preview_lbl.set_margin_start(12)
    preview_lbl.set_margin_end(12)
    preview_lbl.set_margin_bottom(4)
    path_wrap.append(path_group)
    path_wrap.append(preview_lbl)
    col.append(path_wrap)

    col.append(page_section_label("Include" if is_backup else "What to restore"))
    include_group = Adw.PreferencesGroup(
        description=(
            "Turn sections on or off for this run. Your choices are remembered next time."
        ),
    )
    include_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    include_wrap.add_css_class("fu-page-body")
    switches: dict[str, Gtk.Switch] = {}
    for key, opt_title, subtitle, _default in option_defs:
        row = Adw.ActionRow(title=opt_title, subtitle=subtitle)
        try:
            row.set_subtitle_lines(3)
        except AttributeError:
            pass
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(bool(defaults.get(key, True)))
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        include_group.add(row)
        switches[key] = sw

    presets = BACKUP_PRESETS if is_backup else RESTORE_PRESETS
    preset_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    preset_row.set_halign(Gtk.Align.START)
    preset_row.set_margin_top(8)
    preset_row.set_margin_start(12)

    def apply_preset(flags: dict[str, bool]) -> None:
        for key, sw in switches.items():
            sw.set_active(bool(flags.get(key, False)))

    for _pid, plabel, flags in presets:
        pbtn = mk_btn(plabel, "flat", None)
        pbtn.connect("clicked", lambda *_a, f=flags: apply_preset(f))
        preset_row.append(pbtn)
    include_wrap.append(include_group)
    include_wrap.append(preset_row)

    desktop_state = {"v": "all"}
    if is_backup:
        saved_desktop = ""
        dpath = _backup_opts_path("backup")
        if dpath.is_file():
            saved_desktop = read_config_map(dpath).get("desktop", "all")
        if saved_desktop not in {k for k, _ in DESKTOP_PRESETS}:
            saved_desktop = "all"
        desktop_state["v"] = saved_desktop
        desk_grp = Adw.PreferencesGroup(
            title="Desktop environment",
            description="Limit settings capture to one desktop, or keep both.",
        )
        drow = Adw.ActionRow(title="Capture settings for")
        dtoggles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        dtoggles.add_css_class("linked")
        group_btn: Gtk.ToggleButton | None = None
        for key, label in DESKTOP_PRESETS:
            btn = Gtk.ToggleButton(label=label)
            if group_btn is None:
                group_btn = btn
            else:
                try:
                    btn.set_group(group_btn)
                except AttributeError:
                    pass
            btn.set_active(key == desktop_state["v"])
            btn.connect(
                "toggled",
                lambda b, k=key: desktop_state.__setitem__("v", k) if b.get_active() else None,
            )
            dtoggles.append(btn)
        drow.add_suffix(dtoggles)
        desk_grp.add(drow)
        include_wrap.append(desk_grp)

    quick = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    quick.set_halign(Gtk.Align.END)
    quick.set_margin_top(4)

    def set_all(active: bool) -> None:
        for sw in switches.values():
            sw.set_active(active)

    all_btn = mk_btn("Select all", "flat", "checkbox-checked-symbolic")
    none_btn = mk_btn("Select none", "flat", "checkbox-symbolic")
    all_btn.connect("clicked", lambda *_: set_all(True))
    none_btn.connect("clicked", lambda *_: set_all(False))
    quick.append(all_btn)
    quick.append(none_btn)
    quick.set_margin_end(16)

    include_wrap.append(quick)
    col.append(include_wrap)

    # ── Custom paths (backup only) ────────────────────────────────────────
    extra_paths: list[str] = _load_extra_paths() if is_backup else []
    _refresh_start: dict[str, Callable[[], None] | None] = {"fn": None}

    if is_backup:
        col.append(page_section_label("Custom paths"))
        extra_group = Adw.PreferencesGroup(
            description=(
                "Add folders or files to include in this backup "
                "(projects, documents, scripts — whatever you need)."
            ),
        )
        extra_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        extra_wrap.add_css_class("fu-page-body")
        extra_row_widgets: list[Adw.PreferencesRow] = []

        extra_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        extra_actions.set_halign(Gtk.Align.END)
        extra_actions.set_margin_top(6)
        add_folder_btn = mk_btn("Add folder", "fu-secondary", "folder-new-symbolic")
        add_file_btn = mk_btn("Add file", "fu-secondary", "document-new-symbolic")
        extra_actions.append(add_folder_btn)
        extra_actions.append(add_file_btn)

        def persist_extra() -> None:
            _save_extra_paths(extra_paths)

        def rebuild_extra_rows() -> None:
            for old in extra_row_widgets:
                extra_group.remove(old)
            extra_row_widgets.clear()
            if not extra_paths:
                empty = Adw.ActionRow(
                    title="No custom paths yet",
                    subtitle="Add folders or files to archive alongside the sections above.",
                )
                empty.set_sensitive(False)
                extra_group.add(empty)
                extra_row_widgets.append(empty)
            else:
                for path_str in list(extra_paths):
                    p = Path(path_str)
                    kind = "Folder" if p.is_dir() else ("File" if p.is_file() else "Missing — will be skipped")
                    row = Adw.ActionRow(title=path_str, subtitle=kind)
                    try:
                        row.set_subtitle_lines(2)
                    except AttributeError:
                        pass
                    rm = Gtk.Button.new_from_icon_name("list-remove-symbolic")
                    rm.add_css_class("flat")
                    rm.set_valign(Gtk.Align.CENTER)
                    rm.set_tooltip_text("Remove")

                    def _remove(_btn: Gtk.Button, target: str = path_str) -> None:
                        if target in extra_paths:
                            extra_paths.remove(target)
                            persist_extra()
                            rebuild_extra_rows()

                    rm.connect("clicked", _remove)
                    row.add_suffix(rm)
                    extra_group.add(row)
                    extra_row_widgets.append(row)
            fn = _refresh_start["fn"]
            if fn is not None:
                fn()

        def add_path(new_path: str) -> None:
            new_path = (new_path or "").strip()
            if not new_path:
                return
            if new_path not in extra_paths:
                extra_paths.append(new_path)
                persist_extra()
                rebuild_extra_rows()

        def pick_extra_folder(*_a: object) -> None:
            dialog = Gtk.FileDialog(title="Add folder to backup")
            start = Path.home() / "Documents"
            if not start.is_dir():
                start = Path.home()
            dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

            def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    folder = dialog.select_folder_finish(result)
                    if folder:
                        add_path(folder.get_path() or "")
                except GLib.Error:
                    pass

            dialog.select_folder(parent_win, None, on_done)

        def pick_extra_file(*_a: object) -> None:
            dialog = Gtk.FileDialog(title="Add file to backup")
            dialog.set_initial_folder(Gio.File.new_for_path(str(Path.home())))

            def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
                try:
                    gfile = dialog.open_finish(result)
                    if gfile:
                        add_path(gfile.get_path() or "")
                except GLib.Error:
                    pass

            dialog.open(parent_win, None, on_done)

        add_folder_btn.connect("clicked", pick_extra_folder)
        add_file_btn.connect("clicked", pick_extra_file)
        rebuild_extra_rows()
        extra_actions.set_margin_end(16)
        extra_wrap.append(extra_group)
        extra_wrap.append(extra_actions)
        col.append(extra_wrap)

    outer.append(scrolled)

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    actions.add_css_class("fu-actions")
    start_btn = mk_btn(
        "Start backup" if is_backup else "Start restore",
        "suggested-action pill fu-primary",
        "emblem-ok-symbolic" if is_backup else "document-revert-symbolic",
    )
    start_btn.set_sensitive(False)
    start_btn.set_hexpand(True)
    actions.append(start_btn)
    outer.append(pin_page_footer(actions))

    def current_opts() -> dict[str, bool]:
        return {key: sw.get_active() for key, sw in switches.items()}

    def any_include_on() -> bool:
        return any(current_opts().values())

    def refresh_start_sensitive() -> None:
        ok_path = bool(chosen["path"]) and Path(chosen["path"]).is_dir()
        has_work = any_include_on() or (is_backup and bool(extra_paths))
        start_btn.set_sensitive(ok_path and has_work)

    _refresh_start["fn"] = refresh_start_sensitive

    for sw in switches.values():
        sw.connect("notify::active", lambda *_: refresh_start_sensitive())

    def update_preview(path: str) -> None:
        chosen["path"] = path
        path_row.set_title(path if path else "No folder selected")
        ok = bool(path) and Path(path).is_dir()
        if is_backup and ok:
            day = date.today().isoformat()
            preview_lbl.set_text(
                f"Will create: {path}/fedora-setup-{day} (or with a time suffix if that exists)"
            )
        elif (not is_backup) and ok:
            p = Path(path)
            if (p / "manifests").is_dir():
                preview_lbl.set_text("Looks like a valid UrStack / fedora-setup backup.")
            else:
                latest = sorted(p.glob("fedora-setup-*"), reverse=True)
                latest = [d for d in latest if d.is_dir() and (d / "manifests").is_dir()]
                if latest:
                    chosen["path"] = str(latest[0])
                    path_row.set_title(str(latest[0]))
                    preview_lbl.set_text(f"Using latest backup in that folder:\n{latest[0]}")
                else:
                    preview_lbl.set_text(
                        "No manifests/ found — select a fedora-setup-* backup folder."
                    )
                    chosen["path"] = ""
        else:
            preview_lbl.set_text("")
        refresh_start_sensitive()

    def pick_folder(*_a: object) -> None:
        dialog = Gtk.FileDialog(
            title="Choose folder to store the backup"
            if is_backup
            else "Select a fedora-setup-* backup folder"
        )
        start = Path.home() / "Backups"
        if not start.is_dir():
            start = Path.home()
        dialog.set_initial_folder(Gio.File.new_for_path(str(start)))

        def on_done(_dlg: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
            try:
                folder = dialog.select_folder_finish(result)
                if folder:
                    update_preview(folder.get_path() or "")
            except GLib.Error:
                pass

        dialog.select_folder(parent_win, None, on_done)

    pick_btn.connect("clicked", pick_folder)

    def do_start(*_a: object) -> None:
        if not chosen["path"]:
            return
        if is_backup:
            if not (any_include_on() or extra_paths):
                return
            _save_extra_paths(extra_paths)
        elif not any_include_on():
            return
        opts = current_opts()
        extra = {"desktop": desktop_state["v"]} if is_backup else None
        _save_include_defaults(mode, opts, extra)
        prefix = "backup" if is_backup else "restore"
        parts = [_encode_include_opts(opts)]
        if extra:
            parts.extend(f"{k}={v}" for k, v in extra.items())
        encoded = ",".join(p for p in parts if p)
        on_start(f"{prefix}|{chosen['path']}|{encoded}")

    start_btn.connect("clicked", do_start)

    # Navigation back is handled by the page HeaderBar — no duplicate footer Back.
    return outer



def build_settings_content(
    config_file: str,
    *,
    on_rescan: Callable[[], None] | None = None,
    on_saved: Callable[[dict[str, str]], None] | None = None,
    toast_overlay: Adw.ToastOverlay | None = None,
) -> Gtk.Widget:
    cfg_path = Path(config_file).expanduser()
    values = read_config_map(cfg_path)
    for key, _group, _t, _s in SETTING_KEYS:
        values.setdefault(key, setting_default(key))
    values.setdefault("keep_kernels", "3")
    values["appearance"] = normalize_appearance(values.get("appearance"))

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    outer.set_hexpand(True)
    outer.set_vexpand(True)

    scrolled, clamp, page = page_scroll_body(spacing=14)
    enabled_n = sum(
        1
        for k, group, _t, _s in SETTING_KEYS
        if group != "Startup" and values.get(k, "0") == "1"
    )
    chrome = page_chrome_box()
    chrome.append(
        page_hero(
            str(enabled_n),
            "sources on",
            "Sources & behaviour",
            "Only enabled sources are checked on Updates. Scan can rebuild this list from what’s installed.",
            heading="Settings",
            heading_sub="Theme, startup, update sources, and workstation scan.",
            icon_name=page_icon("settings"),
        )
    )
    outer.append(chrome)
    page.append(
        page_callout(
            "Config file",
            str(cfg_path),
        )
    )

    status_banner = Gtk.Label(label="", xalign=0.0, wrap=True)
    status_banner.add_css_class("dim-label")
    status_banner.set_visible(False)
    page.append(status_banner)

    appearance_state = {"v": values["appearance"]}
    appearance_btns: dict[str, Gtk.ToggleButton] = {}
    appearance_suppress = {"v": False}

    def persist_appearance(key: str) -> None:
        key = normalize_appearance(key)
        appearance_state["v"] = key
        apply_appearance(key)
        try:
            write_config_map(cfg_path, {"appearance": key})
        except OSError:
            pass

    def on_appearance_toggle(btn: Gtk.ToggleButton, key: str) -> None:
        if appearance_suppress["v"] or not btn.get_active():
            return
        persist_appearance(key)
        if toast_overlay is not None:
            labels = {"system": "System", "light": "Light", "dark": "Dark"}
            toast_overlay.add_toast(
                Adw.Toast(title=f"Theme: {labels.get(key, key)}")
            )

    app_grp = Adw.PreferencesGroup(
        title="Appearance",
        description="Light and dark themes for UrStack. System follows your desktop.",
    )
    arow = Adw.ActionRow(
        title="Color scheme",
        subtitle="Applies immediately and is saved to your config.",
    )
    try:
        arow.set_subtitle_lines(2)
    except AttributeError:
        pass
    toggles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
    toggles.add_css_class("linked")
    toggles.add_css_class("fu-theme-toggles")
    group_btn: Gtk.ToggleButton | None = None
    for key, label in (("system", "System"), ("light", "Light"), ("dark", "Dark")):
        btn = Gtk.ToggleButton(label=label)
        if group_btn is None:
            group_btn = btn
        else:
            try:
                btn.set_group(group_btn)
            except Exception:  # noqa: BLE001
                pass
        btn.set_active(key == appearance_state["v"])
        btn.connect("toggled", on_appearance_toggle, key)
        toggles.append(btn)
        appearance_btns[key] = btn
    arow.add_suffix(toggles)
    app_grp.add(arow)
    app_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
    app_wrap.add_css_class("fu-page-body")
    app_wrap.append(app_grp)
    page.append(app_wrap)

    switches: dict[str, Gtk.Switch] = {}
    groups: dict[str, Adw.PreferencesGroup] = {}
    group_order: list[str] = []

    for key, group_name, title, subtitle in SETTING_KEYS:
        if group_name not in groups:
            desc = ""
            if group_name == "Startup":
                desc = (
                    "Login, background checks, and desktop notifications. "
                    "Daily check never applies updates on its own."
                )
            elif group_name == "Advisories":
                desc = "Reminders only — UrStack will not install updates for these."
            elif group_name == "Core updates":
                desc = "Main Fedora software channels most people want enabled."
            grp = Adw.PreferencesGroup(title=group_name, description=desc)
            groups[group_name] = grp
            group_order.append(group_name)
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        try:
            row.set_subtitle_lines(4)
        except AttributeError:
            pass
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_active(values.get(key, "0") == "1")
        row.add_suffix(sw)
        row.set_activatable_widget(sw)
        groups[group_name].add(row)
        switches[key] = sw

    # Kernels-to-keep lives with core updates
    core = groups.get("Core updates")
    krow = Adw.ActionRow(
        title="Kernels to keep",
        subtitle=(
            "When pruning is on, keep this many kernels (including the one you are "
            "running) so you can boot an older one if needed. Typical value: 3."
        ),
    )
    try:
        krow.set_subtitle_lines(3)
    except AttributeError:
        pass
    kentry = Gtk.Entry()
    kentry.set_text(str(values.get("keep_kernels", "3")))
    kentry.set_width_chars(4)
    kentry.set_valign(Gtk.Align.CENTER)
    krow.add_suffix(kentry)
    if core is not None:
        core.add(krow)
    else:
        extra = Adw.PreferencesGroup(title="Behaviour")
        extra.add(krow)
        groups["Behaviour"] = extra
        group_order.append("Behaviour")

    for name in group_order:
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        wrap.add_css_class("fu-page-body")
        wrap.append(groups[name])
        page.append(wrap)

    outer.append(scrolled)

    def apply_values_to_ui(new_vals: dict[str, str]) -> None:
        for key, sw in switches.items():
            sw.set_active(new_vals.get(key, setting_default(key)) == "1")
        kentry.set_text(str(new_vals.get("keep_kernels", "3")))
        scheme = normalize_appearance(new_vals.get("appearance"))
        appearance_state["v"] = scheme
        appearance_suppress["v"] = True
        try:
            for key, btn in appearance_btns.items():
                btn.set_active(key == scheme)
        finally:
            appearance_suppress["v"] = False
        apply_appearance(scheme)

    def current_values() -> dict[str, str]:
        out = {key: ("1" if sw.get_active() else "0") for key, sw in switches.items()}
        out["keep_kernels"] = kentry.get_text().strip() or "3"
        out["appearance"] = appearance_state["v"]
        return out

    def do_save(*_a: object) -> None:
        out = current_values()
        write_config_map(cfg_path, out)
        if toast_overlay is not None:
            toast_overlay.add_toast(Adw.Toast(title="Settings saved"))
        if on_saved is not None:
            on_saved(out)

    def do_rescan(*_a: object) -> None:
        # Prefer in-window scan so the main app never disappears behind a tiny dialog.
        if on_rescan is not None:
            # Legacy standalone settings window still exits to let the shell scan.
            on_rescan()
            return

        rescan_btn.set_sensitive(False)
        save_btn.set_sensitive(False)
        rescan_btn.set_label("Scanning…")
        status_banner.set_visible(True)
        status_banner.set_text("Scanning workstation…")
        status_banner.remove_css_class("error")

        def work() -> None:
            ok, enabled, detail = False, [], "scan did not complete"
            try:
                ok, enabled, detail = run_workstation_rescan(cfg_path)
            finally:
                # finish_scan re-enables the buttons and clears the banner, so it
                # has to run even if the rescan raised.
                GLib.idle_add(finish_scan, ok, enabled, detail)

        def finish_scan(ok: bool, enabled: list[str], detail: str) -> bool:
            rescan_btn.set_sensitive(True)
            save_btn.set_sensitive(True)
            rescan_btn.set_label("Scan workstation")
            if not ok:
                status_banner.add_css_class("error")
                status_banner.set_text(f"Scan failed: {detail or 'unknown error'}")
                if toast_overlay is not None:
                    toast_overlay.add_toast(Adw.Toast(title="Workstation scan failed"))
                return False

            new_vals = read_config_map(cfg_path)
            apply_values_to_ui(new_vals)
            pretty = ", ".join(enabled) if enabled else "(none)"
            status_banner.set_text(f"Workstation scanned and config updated.\nEnabled: {pretty}")
            if toast_overlay is not None:
                toast_overlay.add_toast(Adw.Toast(title="Workstation scanned — settings updated"))
            if on_saved is not None:
                # Notify shell (e.g. Backup button visibility) without leaving Settings
                on_saved(current_values())
            return False

        threading.Thread(target=work, daemon=True).start()

    actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    actions.add_css_class("fu-actions")
    save_btn = mk_btn("Save", "suggested-action pill fu-primary", "document-save-symbolic")
    save_btn.set_hexpand(True)
    save_btn.connect("clicked", do_save)
    actions.append(save_btn)
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    row.set_homogeneous(True)
    rescan_btn = mk_btn("Scan workstation", "fu-secondary", "view-refresh-symbolic")
    rescan_btn.connect("clicked", do_rescan)
    row.append(rescan_btn)
    actions.append(row)
    outer.append(pin_page_footer(actions))
    return outer



def build_embedded_progress_content(
    *,
    title: str,
    status: str = "Starting…",
    pulsate: bool = False,
    cancellable: bool = False,
) -> tuple[
    Gtk.Widget,
    Callable[[str], None],
    Callable[[float | None], None],
    Callable[[str], None],
    Callable[[bool, str], None],
    Gtk.Button,
    Gtk.Button | None,
]:
    """In-window progress UI (status + bar + log). Returns content and setters.

    cancellable adds a Cancel button; the caller owns what it does. Only jobs
    that can be stopped at any point should set it, which rules out anything
    driving a package transaction.
    """
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
    hero.add_css_class("fu-hero")
    hero.append(app_icon_image(48))
    titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    titles.set_hexpand(True)
    t = Gtk.Label(label=title, xalign=0.0)
    t.add_css_class("fu-hero-title")
    titles.append(t)
    status_lbl = Gtk.Label(label=status, xalign=0.0, wrap=True)
    status_lbl.add_css_class("fu-hero-sub")
    titles.append(status_lbl)
    hero.append(titles)
    outer.append(hero)

    bar_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    bar_wrap.set_margin_start(20)
    bar_wrap.set_margin_end(20)
    bar_wrap.set_margin_bottom(4)
    progress = Gtk.ProgressBar()
    progress.set_show_text(not pulsate)
    progress.set_fraction(0.0)
    if not pulsate:
        progress.set_text("0%")
    pulse_state = {"id": 0, "alive": True}
    if pulsate:
        progress.pulse()

        def _pulse() -> bool:
            if not pulse_state["alive"]:
                return False
            progress.pulse()
            return True

        pulse_state["id"] = GLib.timeout_add(120, _pulse)
    bar_wrap.append(progress)
    outer.append(bar_wrap)

    scrolled = Gtk.ScrolledWindow()
    scrolled.set_vexpand(True)
    scrolled.set_margin_start(16)
    scrolled.set_margin_end(16)
    scrolled.set_margin_top(8)
    scrolled.set_margin_bottom(8)
    clamp = wide_clamp()
    log_lbl = Gtk.Label(label="", xalign=0.0, wrap=True, selectable=True)
    log_lbl.add_css_class("fu-mono")
    log_lbl.set_valign(Gtk.Align.START)
    clamp.set_child(log_lbl)
    scrolled.set_child(clamp)
    outer.append(scrolled)

    done_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    done_row.add_css_class("fu-actions")
    done_row.set_halign(Gtk.Align.END)
    cancel_btn: Gtk.Button | None = None
    if cancellable:
        cancel_btn = mk_btn("Cancel", "flat fu-secondary", "process-stop-symbolic")
        done_row.append(cancel_btn)
    done_btn = mk_btn("Done", "suggested-action pill fu-primary", "emblem-ok-symbolic")
    done_btn.set_sensitive(False)
    done_row.append(done_btn)
    outer.append(done_row)

    log_buf: list[str] = []

    def set_status(text: str) -> None:
        status_lbl.set_text(text)

    def set_fraction(frac: float | None) -> None:
        if frac is None:
            progress.pulse()
            return
        progress.set_fraction(max(0.0, min(1.0, frac)))
        if not pulsate:
            progress.set_text(f"{int(round(frac * 100))}%")

    def append_log(line: str) -> None:
        line = line.rstrip()
        if not line:
            return
        log_buf.append(line)
        if len(log_buf) > 400:
            del log_buf[:100]
        log_lbl.set_text("\n".join(log_buf[-120:]))

    def mark_finished(ok: bool, summary: str = "") -> None:
        pulse_state["alive"] = False
        if pulse_state["id"]:
            try:
                GLib.source_remove(pulse_state["id"])
            except Exception:  # noqa: BLE001
                pass
            pulse_state["id"] = 0
        set_fraction(1.0)
        set_status(summary or ("Finished" if ok else "Failed"))
        if cancel_btn is not None:
            cancel_btn.set_visible(False)
        done_btn.set_sensitive(True)

    return outer, set_status, set_fraction, append_log, mark_finished, done_btn, cancel_btn


def mode_shell(args: argparse.Namespace) -> int:
    """Single-window app: Overview ↔ Updates / Apps / Health / … via NavigationView."""

    def build(app: Adw.Application, state: dict) -> None:
        win = make_window(app, args.title)
        toast = Adw.ToastOverlay()
        shell_root = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        shell_root.set_hexpand(True)
        shell_root.set_vexpand(True)
        nav = Adw.NavigationView()
        nav.set_hexpand(True)
        nav.set_vexpand(True)
        toast.set_child(shell_root)
        win.set_content(toast)

        result = {"action": "close"}
        job_busy = {"v": False}
        session = {
            "has_updates": bool(args.has_updates),
            "raw": read_text(args.file, None),
            "nav": "overview",
            "checking": False,
            "checking_updates": False,
            "checking_health": False,
        }
        import tempfile as _tempfile

        _health_status = Path(_tempfile.mkdtemp(prefix="urstack-health-")) / "health.status"
        session["health_status"] = str(_health_status)
        sidebar_ctl: dict[str, Callable[..., object]] = {}
        health_ctl: dict[str, Callable[..., object]] = {}
        scan_inflight = {"updates": False, "health": False}

        closing = {"confirmed": False, "prompt": False}

        def finish(action: str) -> None:
            result["action"] = action
            closing["confirmed"] = True
            win.close()

        def prompt_close() -> None:
            if closing["confirmed"] or closing["prompt"]:
                return
            if not job_busy["v"]:
                finish("close")
                return
            closing["prompt"] = True
            try:
                win.set_visible(True)
                win.present()
            except Exception:  # noqa: BLE001
                pass
            try:
                dialog = Adw.AlertDialog(
                    heading="Close UrStack?",
                    body=(
                        "A scan or install is still running. Closing now may interrupt it.\n\n"
                        "Quit UrStack anyway?"
                    ),
                )
                dialog.add_response("cancel", "Cancel")
                dialog.add_response("close", "Close")
                dialog.set_response_appearance(
                    "close", Adw.ResponseAppearance.DESTRUCTIVE
                )
                dialog.set_default_response("cancel")
                dialog.set_close_response("cancel")

                def on_resp(_d: Adw.AlertDialog, response: str) -> None:
                    closing["prompt"] = False
                    if response == "close":
                        finish("close")
                        return
                    set_active = sidebar_ctl.get("set_active")
                    if callable(set_active):
                        set_active(str(session.get("nav") or "overview"))

                dialog.connect("response", on_resp)
                dialog.present(win)
            except Exception:  # noqa: BLE001
                closing["prompt"] = False
                finish("close")

        nav_handlers: dict[str, Callable[..., object]] = {}

        overview_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        overview_slot.set_vexpand(True)
        overview_slot.set_hexpand(True)

        hub_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        hub_slot.set_vexpand(True)
        hub_slot.set_hexpand(True)

        def go_overview() -> None:
            try:
                page = nav.find_page("overview")
                if page is not None:
                    nav.pop_to_page(page)
            except Exception:  # noqa: BLE001
                pass
            set_active = sidebar_ctl.get("set_active")
            if callable(set_active):
                set_active("overview")
            session["nav"] = "overview"
            rebuild_overview()

        def go_home() -> None:
            """Open the Updates page (sidebar id remains 'home')."""
            push_updates()

        def on_hub_action(action: str) -> None:
            if action == "overview":
                go_overview()
                return
            if action == "home":
                go_home()
                return
            if action == "close":
                prompt_close()
                return
            handler = nav_handlers.get(action)
            if handler is not None:
                handler()
                return
            finish(action)

        def on_sidebar_action(action: str) -> None:
            # Ignore re-select of the same destination unless we need to surface it
            if action == session.get("nav") and action not in {
                "apply",
                "home",
                "overview",
            }:
                # Still ensure the page is visible (e.g. after job-progress)
                handler = nav_handlers.get(action)
                if handler is not None:
                    handler()
                return
            on_hub_action(action)

        def _sync_checking_flag() -> None:
            session["checking_updates"] = bool(scan_inflight["updates"])
            session["checking_health"] = bool(scan_inflight["health"])
            session["checking"] = bool(
                scan_inflight["updates"] or scan_inflight["health"]
            )
            job_busy["v"] = bool(session["checking"])

        def rebuild_overview() -> None:
            _sync_checking_flag()
            while (child := overview_slot.get_first_child()) is not None:
                overview_slot.remove(child)
            overview_slot.append(
                build_overview_content(
                    raw=session["raw"],
                    has_updates=bool(session["has_updates"]),
                    status_file=str(getattr(args, "status_file", "") or ""),
                    health_status_file=str(session.get("health_status", "") or ""),
                    runs_dir=str(getattr(args, "runs_dir", "") or ""),
                    log_file=str(getattr(args, "log_file", "") or ""),
                    config_file=str(getattr(args, "config_file", "") or ""),
                    checking=bool(session.get("checking")),
                    checking_updates=bool(session.get("checking_updates")),
                    checking_health=bool(session.get("checking_health")),
                    on_action=on_hub_action,
                    on_refresh=start_overview_refresh,
                )
            )

        def run_background_health_scan(*, toast_on_done: bool = False) -> None:
            """Scan health without leaving Overview (used at launch / Refresh)."""
            if scan_inflight["health"]:
                return
            scan_inflight["health"] = True
            _sync_checking_flag()
            hs = str(session.get("health_status") or "")
            if not hs:
                scan_inflight["health"] = False
                _sync_checking_flag()
                return
            Path(hs).parent.mkdir(parents=True, exist_ok=True)

            def work() -> None:
                def done() -> bool:
                    scan_inflight["health"] = False
                    remount = health_ctl.get("remount")
                    if callable(remount):
                        try:
                            remount()
                        except Exception:  # noqa: BLE001
                            pass
                    rebuild_overview()
                    if toast_on_done:
                        toast.add_toast(Adw.Toast(title="Health scan complete"))
                    return False

                try:
                    run_health_scan_subprocess(hs)
                finally:
                    # done() clears scan_inflight. If a crash in the worker skipped
                    # it, the flag would stay set and every later scan would be
                    # refused with "a scan is already running".
                    GLib.idle_add(done)

            threading.Thread(target=work, daemon=True).start()

        def _refresh_updates_async(
            fail_message: str, fail_toast: str, *, force_metadata: bool = False
        ) -> None:
            """Re-scan updates off the main thread and fold the result into the UI.

            First launch, the Refresh button and the Overview refresh differ only
            in what they say when the scan fails. Manual refresh also forces a
            metadata pull so cached DNF/Flatpak stamps are not reused.
            """

            def work() -> None:
                ok = has_u = False
                raw = err = ""

                def done() -> bool:
                    scan_inflight["updates"] = False
                    if not ok:
                        rebuild_hub(raw=err or fail_message, has_updates=False)
                        rebuild_overview()
                        tray_say("idle")
                        toast.add_toast(Adw.Toast(title=fail_toast))
                        return False
                    rebuild_hub(raw=raw, has_updates=has_u)
                    rebuild_overview()
                    tray_say("updates" if has_u else "idle")
                    toast.add_toast(
                        Adw.Toast(
                            title="Updates available" if has_u else "You're up to date"
                        )
                    )
                    return False

                try:
                    ok, has_u, raw, err = refresh_hub_from_session(
                        force_metadata=force_metadata
                    )
                finally:
                    # done() clears scan_inflight; skipping it would leave every
                    # later refresh refused as "a scan is already running".
                    GLib.idle_add(done)

            threading.Thread(target=work, daemon=True).start()

        def start_overview_refresh() -> None:
            """Refresh from Overview — updates + health in parallel."""
            if scan_inflight["updates"] or scan_inflight["health"]:
                toast.add_toast(Adw.Toast(title="A scan is already running"))
                return
            try:
                page = nav.find_page("overview")
                if page is not None:
                    nav.pop_to_page(page)
            except Exception:  # noqa: BLE001
                pass
            set_active = sidebar_ctl.get("set_active")
            if callable(set_active):
                set_active("overview")
            session["nav"] = "overview"

            scan_inflight["updates"] = True
            _sync_checking_flag()
            tray_say("checking")
            rebuild_overview()
            run_background_health_scan(toast_on_done=False)

            _refresh_updates_async(
                "Could not refresh updates.",
                "Update check failed",
                force_metadata=True,
            )

        def start_manual_refresh() -> None:
            """Refresh button on Updates — re-scan in place (no splash window)."""
            if scan_inflight["updates"]:
                toast.add_toast(Adw.Toast(title="A scan is already running"))
                return
            scan_inflight["updates"] = True
            _sync_checking_flag()
            tray_say("checking")
            rebuild_hub()

            _refresh_updates_async(
                "Could not refresh updates.",
                "Refresh failed",
                force_metadata=True,
            )

        def rebuild_hub(raw: str | None = None, has_updates: bool | None = None) -> None:
            if raw is not None:
                session["raw"] = raw
            if has_updates is not None:
                session["has_updates"] = bool(has_updates)
            while (child := hub_slot.get_first_child()) is not None:
                hub_slot.remove(child)
            content, _rebuild_nav = build_hub_content(
                raw=session["raw"],
                has_updates=bool(session["has_updates"]),
                enable_backup=True,
                on_action=on_hub_action,
                on_backup_visibility=None,
                show_nav_buttons=False,
                on_refresh=start_manual_refresh,
                checking=bool(scan_inflight["updates"]),
            )
            hub_slot.append(content)
            set_has = sidebar_ctl.get("set_has_updates")
            if callable(set_has):
                set_has(bool(session["has_updates"]))

        def push_updates(*_a: object) -> bool:
            if hub_slot.get_first_child() is None:
                rebuild_hub()
            push_page("Updates", hub_slot, "Sources & apply", tag="home")
            return False

        pending_check = bool(int(getattr(args, "pending_check", 0) or 0))

        if pending_check:
            scan_inflight["updates"] = True
            scan_inflight["health"] = True
            _sync_checking_flag()
            rebuild_overview()
        else:
            rebuild_hub()
            rebuild_overview()

        sidebar, set_active, set_has_updates = build_shell_sidebar(
            on_sidebar_action,
            has_updates=bool(session["has_updates"]),
            config_file=str(getattr(args, "config_file", "") or ""),
        )
        sidebar_ctl["set_active"] = set_active
        sidebar_ctl["set_has_updates"] = set_has_updates
        shell_root.append(sidebar)
        shell_root.append(nav)

        TAG_TO_NAV = {
            "overview": "overview",
            "home": "home",
            "apply": "home",
            "apps": "apps",
            "health": "health",
            "look": "look",
            "backup": "backup",
            "restore": "restore",
            "settings": "settings",
            "history": "log",
            "runs": "runs",
            "job-progress": "",  # keep previous
        }

        def sync_sidebar_from_nav(*_a: object) -> None:
            try:
                page = nav.get_visible_page()
            except Exception:  # noqa: BLE001
                page = None
            tag = ""
            if page is not None:
                try:
                    tag = page.get_tag() or ""
                except Exception:  # noqa: BLE001
                    tag = ""
            nav_id = TAG_TO_NAV.get(tag, "")
            if not nav_id:
                return
            session["nav"] = nav_id
            set_active(nav_id)

        try:
            nav.connect("notify::visible-page", sync_sidebar_from_nav)
        except Exception:  # noqa: BLE001
            pass

        def push_page(title: str, content: Gtk.Widget, subtitle: str | None = None, *, tag: str) -> None:
            """Push a child page; jump to an existing same-tag page if already stacked."""
            try:
                existing = nav.find_page(tag)
            except Exception:  # noqa: BLE001
                existing = None
            if existing is not None:
                try:
                    nav.pop_to_page(existing)
                    nav_id = TAG_TO_NAV.get(tag, tag)
                    if nav_id:
                        session["nav"] = nav_id
                        set_active(nav_id)
                    return
                except Exception:  # noqa: BLE001
                    pass
            nav.push(make_nav_page(title, content, subtitle, tag=tag))
            nav_id = TAG_TO_NAV.get(tag, tag)
            if nav_id:
                session["nav"] = nav_id
                set_active(nav_id)

        def refresh_hub_from_session(*, force_metadata: bool = False) -> tuple[bool, bool, str, str]:
            """Re-run checks into the live check-dir and rewrite results/sections files.

            Returns (ok, has_updates, raw, error).
            """
            check_dir = (getattr(args, "check_dir", "") or "").strip()
            results_file = (getattr(args, "file", "") or "").strip()
            sections_file = (getattr(args, "sections_file", "") or "").strip()
            if not check_dir or not Path(check_dir).is_dir():
                return False, False, session["raw"], "Update session expired — reopen UrStack"
            cmd = _urstack_command() + [
                "--refresh-check",
                "--check-dir",
                check_dir,
            ]
            if results_file:
                cmd += ["--results-file", results_file]
            if sections_file:
                cmd += ["--sections-file", sections_file]
            env = os.environ.copy()
            root = Path(__file__).resolve().parents[2]
            env["FEDORA_UPDATES_ROOT"] = str(root)
            env["URSTACK_ROOT"] = str(root)
            env["STACKUP_ROOT"] = str(root)
            env["URSTACK_EMBEDDED_PROGRESS"] = "1"
            if force_metadata:
                env["URSTACK_FORCE_METADATA"] = "1"
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, bool(session["has_updates"]), session["raw"], str(exc)
            if proc.returncode not in (0, 1):
                err = (proc.stderr or proc.stdout or "refresh failed").strip()
                return False, bool(session["has_updates"]), session["raw"], err[:300]
            has_updates = False
            for line in (proc.stdout or "").splitlines():
                if line.startswith("HAS_UPDATES="):
                    has_updates = line.split("=", 1)[1].strip() == "1"
                    break
            raw = read_text(results_file, None) if results_file else session["raw"]
            if (raw or "").lstrip().lower().startswith("nothing to update"):
                has_updates = False
            return True, has_updates, raw, ""

        def apply_hub_refresh(ok: bool, has_updates: bool, raw: str, err: str) -> bool:
            if ok:
                rebuild_hub(raw=raw, has_updates=has_updates)
                rebuild_overview()
                tray_say("updates" if has_updates else "idle")
                toast.add_toast(
                    Adw.Toast(
                        title=(
                            "Updates refreshed"
                            if has_updates
                            else "You're up to date"
                        )
                    )
                )
            elif err:
                toast.add_toast(Adw.Toast(title=f"Could not refresh: {err[:80]}"))
            return False


        def run_initial_check() -> bool:
            """Background first-launch: updates + health in parallel."""

            _refresh_updates_async(
                "Could not finish the update check.",
                "Update check failed",
            )

            # Health scan already marked inflight; kick the worker (no double-set).
            def health_work() -> None:
                def done() -> bool:
                    scan_inflight["health"] = False
                    remount = health_ctl.get("remount")
                    if callable(remount):
                        try:
                            remount()
                        except Exception:  # noqa: BLE001
                            pass
                    rebuild_overview()
                    return False

                try:
                    hs = str(session.get("health_status") or "")
                    if hs:
                        Path(hs).parent.mkdir(parents=True, exist_ok=True)
                        run_health_scan_subprocess(hs)
                finally:
                    GLib.idle_add(done)

            threading.Thread(target=health_work, daemon=True).start()
            return False

        if pending_check:
            GLib.idle_add(run_initial_check)

        def run_embedded_job(
            *,
            title: str,
            argv: list[str],
            env_extra: dict[str, str] | None = None,
            pulsate: bool = False,
            success_toast: str = "Done",
            fail_toast: str = "Failed",
            refresh_hub: bool = False,
            on_complete: Callable[[bool], None] | None = None,
            done_goes_home: bool = True,
            auto_complete: bool = False,
            cancellable: bool = False,
        ) -> None:
            """Keep the main window open and show progress for a long-running urstack job.

            cancellable is only safe for jobs that copy files. A job that drives
            a package transaction or writes into /etc must not be interruptible
            partway through.
            """
            if job_busy["v"]:
                toast.add_toast(Adw.Toast(title="Another job is already running"))
                return
            job_busy["v"] = True
            completed = {"v": False}
            cancelled = {"v": False}
            running: dict[str, subprocess.Popen | None] = {"proc": None}

            built = build_embedded_progress_content(
                title=title, pulsate=pulsate, cancellable=cancellable
            )
            (
                content,
                set_status,
                set_fraction,
                append_log,
                mark_finished,
                done_btn,
                cancel_btn,
            ) = built

            def on_cancel_click(*_a: object) -> None:
                if cancelled["v"]:
                    return
                cancelled["v"] = True
                if cancel_btn is not None:
                    cancel_btn.set_sensitive(False)
                set_status("Cancelling…")
                append_log("Cancelling…")
                # Off the main loop: terminate() waits out the kill grace period.
                threading.Thread(
                    target=terminate_process_group,
                    args=(running["proc"],),
                    daemon=True,
                ).start()

            if cancel_btn is not None:
                cancel_btn.connect("clicked", on_cancel_click)

            def on_done_click(*_a: object) -> None:
                if on_complete is not None and not completed["v"]:
                    completed["v"] = True
                    on_complete(True)
                    return
                if done_goes_home:
                    go_overview()
                    return
                try:
                    page = nav.find_page("job-progress")
                    visible = nav.get_visible_page()
                    if page is not None and visible == page:
                        nav.pop()
                except Exception:  # noqa: BLE001
                    pass

            done_btn.connect("clicked", on_done_click)
            try:
                old = nav.find_page("job-progress")
            except Exception:  # noqa: BLE001
                old = None
            if old is not None:
                try:
                    nav.pop_to_page(old)
                    nav.pop()
                except Exception:  # noqa: BLE001
                    pass
            nav.push(make_nav_page(title, content, "Please wait", tag="job-progress"))

            root = Path(__file__).resolve().parents[2]
            meta: dict[str, str] = {}

            def handle_line(line: str) -> bool:
                s = line.rstrip("\n")
                if s.startswith("DEST="):
                    meta["dest"] = s[5:].strip()
                elif s.startswith("REPORT="):
                    meta["report"] = s[7:].strip()
                elif s.startswith("FAILS="):
                    meta["fails"] = s[6:].strip()
                elif s.startswith("#"):
                    set_status(s[1:].strip() or "Working…")
                    append_log(s[1:].strip())
                elif s.strip().isdigit():
                    set_fraction(int(s.strip()) / 100.0)
                elif s.strip():
                    append_log(s)
                return False

            def finish_job(code: int) -> bool:
                ok = code == 0 and not cancelled["v"]
                summary = success_toast if ok else fail_toast
                if cancelled["v"]:
                    summary = f"{title} cancelled"
                    partial = meta.get("dest")
                    if partial:
                        append_log("")
                        append_log(f"Stopped. Partial folder: {partial}")
                        append_log(
                            "It is marked incomplete, so a restore will refuse it. "
                            "Delete it when you no longer need it."
                        )
                elif ok and meta.get("dest") and title == "Backup":
                    summary = f"Backup saved to {meta['dest']}"
                elif ok and meta.get("dest") and title == "Saving look pack":
                    summary = f"Look pack saved to {meta['dest']}"
                elif ok and meta.get("report") and title == "Restore":
                    fails = meta.get("fails", "0")
                    summary = (
                        f"Restore finished ({fails} failed step(s))"
                        if fails not in ("", "0")
                        else "Restore finished"
                    )

                def finalize(final_summary: str = summary) -> bool:
                    job_busy["v"] = False
                    if ok and meta.get("dest") and title == "Backup":
                        try:
                            _save_last_backup(meta["dest"])
                        except Exception:  # noqa: BLE001
                            pass
                    mark_finished(ok, final_summary)
                    toast.add_toast(Adw.Toast(title=final_summary[:120]))
                    if (
                        auto_complete
                        and on_complete is not None
                        and not refresh_hub
                        and not completed["v"]
                    ):
                        completed["v"] = True
                        on_complete(ok)
                    return False

                if not refresh_hub:
                    return finalize()

                set_status("Refreshing update list…")
                append_log("Refreshing update list…")

                def refresh_work() -> None:
                    rok = has_u = False
                    raw = err = ""

                    def after() -> bool:
                        job_busy["v"] = False
                        apply_hub_refresh(rok, has_u, raw, err)
                        go_overview()
                        toast.add_toast(Adw.Toast(title=summary[:120]))
                        return False

                    try:
                        rok, has_u, raw, err = refresh_hub_from_session(
                            force_metadata=True
                        )
                    finally:
                        # after() clears job_busy; skipping it wedges the job UI.
                        GLib.idle_add(after)

                threading.Thread(target=refresh_work, daemon=True).start()
                return False

            def work() -> None:
                # Anything that escapes here would leave the job page showing a
                # backup or upgrade still running, with no way to start another.
                code = 1
                env = os.environ.copy()
                env["FEDORA_UPDATES_ROOT"] = str(root)
                env["URSTACK_ROOT"] = str(root)
                env["STACKUP_ROOT"] = str(root)
                env["URSTACK_EMBEDDED_PROGRESS"] = "1"
                if env_extra:
                    env.update(env_extra)
                try:
                    try:
                        proc = subprocess.Popen(
                            argv,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            env=env,
                            bufsize=1,
                            # Own session, so Cancel can signal the whole tree
                            # rather than just the top-level script.
                            start_new_session=True,
                        )
                    except OSError as exc:
                        GLib.idle_add(append_log, str(exc))
                        return
                    running["proc"] = proc
                    # Cancel may have been clicked while the job was starting, in
                    # which case it signalled a process that did not exist yet.
                    if cancelled["v"]:
                        terminate_process_group(proc)
                    assert proc.stdout is not None
                    for line in proc.stdout:
                        GLib.idle_add(handle_line, line)
                    # No overall timeout: an upgrade or backup is legitimately long.
                    # But once stdout has closed the job is finished, so a process
                    # that still will not exit is wedged and must not hang the thread.
                    try:
                        code = proc.wait(timeout=JOB_EXIT_TIMEOUT)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        code = proc.wait()
                        GLib.idle_add(
                            append_log,
                            f"Job did not exit within {JOB_EXIT_TIMEOUT}s "
                            "after finishing output; terminated.",
                        )
                finally:
                    GLib.idle_add(finish_job, code)

            threading.Thread(target=work, daemon=True).start()

        def return_to_look(_ok: bool = False) -> None:
            try:
                job = nav.find_page("job-progress")
                if job is not None and nav.get_visible_page() == job:
                    nav.pop()
            except Exception:  # noqa: BLE001
                pass
            set_act = sidebar_ctl.get("set_active")
            if callable(set_act):
                set_act("look")
            session["nav"] = "look"

        def start_look_export(path: str, include: str) -> None:
            argv = _look_py_command() + ["export", "--out", path]
            if include:
                argv += ["--include", include]
            run_embedded_job(
                title="Saving look pack",
                argv=argv,
                pulsate=False,
                success_toast="Look pack saved",
                fail_toast="Could not save the look pack",
                cancellable=True,
                on_complete=return_to_look,
                done_goes_home=False,
                auto_complete=True,
            )

        def start_look_install(path: str) -> None:
            run_embedded_job(
                title="Installing theme",
                argv=_look_py_command() + ["install", path],
                pulsate=False,
                success_toast="Theme installed",
                fail_toast="Could not install this archive",
                on_complete=return_to_look,
                done_goes_home=False,
                auto_complete=True,
            )

        def start_look_store_install(host: str, content_id: str) -> None:
            if not host or not content_id:
                return
            run_embedded_job(
                title="Downloading theme",
                argv=_look_py_command()
                + ["download-install", "--host", host, "--id", content_id],
                pulsate=False,
                success_toast="Theme installed",
                fail_toast="Could not download this theme",
                on_complete=return_to_look,
                done_goes_home=False,
                auto_complete=True,
            )

        def start_backup_or_restore(action: str) -> None:
            """Parse backup|path|opts / restore|path|opts and run in-window."""
            if action.startswith("backup|"):
                payload = action[len("backup|") :]
                mode = "backup"
            elif action.startswith("restore|"):
                payload = action[len("restore|") :]
                mode = "restore"
            else:
                finish(action)
                return
            path, _, opts = payload.partition("|")
            path = path.strip()
            if not path:
                toast.add_toast(Adw.Toast(title="Choose a folder first"))
                return
            env_extra = {}
            if opts.strip():
                env_extra["URSTACK_BACKUP_OPTS"] = opts.strip()
            if mode == "backup":
                extras = _load_extra_paths()
                if extras:
                    env_extra["URSTACK_BACKUP_EXTRA_PATHS"] = "\n".join(extras)
                run_embedded_job(
                    title="Backup",
                    argv=_urstack_command() + ["--backup", path],
                    env_extra=env_extra,
                    pulsate=False,
                    success_toast="Backup complete",
                    fail_toast="Backup failed",
                    # Only copies files out to a new folder, so it can stop at
                    # any point without leaving the system in a half-state.
                    cancellable=True,
                )
            else:
                run_embedded_job(
                    title="Restore",
                    argv=_urstack_command() + ["--restore", path],
                    env_extra=env_extra,
                    pulsate=True,
                    success_toast="Restore complete",
                    fail_toast="Restore failed",
                )

        def start_apply_sections(selected: list[str]) -> None:
            check_dir = (getattr(args, "check_dir", "") or "").strip()
            if not check_dir or not Path(check_dir).is_dir():
                toast.add_toast(Adw.Toast(title="Update session expired — reopen UrStack"))
                return
            if not selected:
                toast.add_toast(Adw.Toast(title="Select at least one section"))
                return
            try:
                nav.pop()
            except Exception:  # noqa: BLE001
                pass
            sections = "|".join(selected)
            run_embedded_job(
                title="Applying updates",
                argv=_urstack_command()
                + ["--apply-sections", sections, "--check-dir", check_dir],
                pulsate=False,
                success_toast="Updates finished",
                fail_toast="Updates finished with errors",
                refresh_hub=True,
            )

        def push_apply(*_a: object) -> bool:
            sections_file = (getattr(args, "sections_file", "") or "").strip()
            check_dir = (getattr(args, "check_dir", "") or "").strip()
            items = parse_items_file(sections_file) if sections_file and Path(sections_file).is_file() else []
            cfg_now = read_config_map(Path(args.config_file)) if getattr(args, "config_file", "") else {}
            apply_fw_on = cfg_now.get("apply_fw", setting_default("apply_fw")) == "1"
            for it in items:
                if it.item_id == "fw":
                    it.checked = apply_fw_on
            if not items:
                if check_dir and Path(check_dir).is_dir():
                    run_embedded_job(
                        title="Applying updates",
                        argv=_urstack_command()
                        + ["--apply-sections", "all", "--check-dir", check_dir],
                        pulsate=False,
                        success_toast="Updates finished",
                        fail_toast="Updates finished with errors",
                        refresh_hub=True,
                    )
                else:
                    toast.add_toast(Adw.Toast(title="Nothing to apply"))
                return False

            def on_cancel() -> None:
                try:
                    nav.pop()
                except Exception:  # noqa: BLE001
                    pass

            apply_sub = "Selected sections will run in order. Progress stays in this window."
            if any(it.item_id == "fw" for it in items):
                apply_sub = (
                    "Firmware is unchecked unless you enable Apply firmware in Settings. "
                    "Tick it here for a one-off; a flash may need a reboot."
                )
            content = build_checklist_content(
                items,
                heading="Select which updates to apply",
                subheading=apply_sub,
                ok_label="Apply",
                on_confirm=start_apply_sections,
                on_cancel=on_cancel,
            )
            push_page("Apply updates", content, "Select sections", tag="apply")
            return False

        def start_catalog_install(action: str) -> None:
            if not (
                action.startswith("install|")
                or action.startswith("install-batch|")
                or action.startswith("uninstall|")
            ):
                finish(action)
                return
            env_extra = {
                "URSTACK_CATALOG_STATUS": str(getattr(args, "status_file", "") or ""),
            }
            if action.startswith("uninstall|"):
                title = "Uninstalling"
                success = "Uninstall finished"
                fail = "Uninstall finished with errors"
            elif action.startswith("install-batch|"):
                title = "Installing apps"
                success = "Install finished"
                fail = "Install finished with errors"
            else:
                title = "Installing"
                success = "Install finished"
                fail = "Install finished with errors"
            run_embedded_job(
                title=title,
                argv=_urstack_command() + ["--catalog-choice", action],
                env_extra=env_extra,
                pulsate=False,
                success_toast=success,
                fail_toast=fail,
                done_goes_home=False,
            )

        overview_page = make_nav_page("", overview_slot, None, tag="overview")
        nav.add(overview_page)

        def push_apps(*_a: object) -> bool:
            content = build_catalog_content(
                args.status_file,
                on_install=start_catalog_install,
                on_back=None,
                category=getattr(args, "category", "") or "",
            )
            push_page("Apps", content, "Popular software", tag="apps")
            return False

        health_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        health_slot.set_hexpand(True)
        health_slot.set_vexpand(True)

        def _health_status_path() -> str:
            return str(session["health_status"])

        def _mount_health_content(*, scanning: bool = False) -> None:
            while health_slot.get_first_child() is not None:
                health_slot.remove(health_slot.get_first_child())

            def do_apply(ids: list[str], make_rp: bool = True) -> None:
                if not ids:
                    return
                csv = ",".join(ids)
                hs = _health_status_path()

                def after_apply(_ok: bool) -> None:
                    run_health_scan(embedded=True, after=True)

                env_extra = {"URSTACK_HEALTH_STATUS": hs}
                if not make_rp:
                    env_extra["URSTACK_HEALTH_SKIP_RESTORE_POINT"] = "1"

                run_embedded_job(
                    title="Applying health fixes",
                    argv=_urstack_command()
                    + ["--health-apply", csv, "--health-status", hs],
                    env_extra=env_extra,
                    pulsate=False,
                    success_toast="Health fixes applied",
                    fail_toast="Health apply finished with errors",
                    on_complete=after_apply,
                    done_goes_home=False,
                    auto_complete=True,
                )

            def do_refresh() -> None:
                run_health_scan(embedded=True, after=False)

            def do_create_rp() -> None:
                def after(_ok: bool) -> None:
                    _mount_health_content(scanning=False)
                    push_page(
                        "Health", health_slot, "Workstation diagnostics", tag="health"
                    )

                run_embedded_job(
                    title="Creating restore point",
                    argv=_urstack_command() + ["--health-restore-point"],
                    pulsate=True,
                    success_toast="Restore point created",
                    fail_toast="Could not create restore point",
                    on_complete=after,
                    done_goes_home=False,
                    auto_complete=True,
                    # Copies current config aside; nothing is applied yet.
                    cancellable=True,
                )

            def do_restore_latest() -> None:
                if parent_win := win:

                    def confirmed() -> None:
                        def after(_ok: bool) -> None:
                            run_health_scan(embedded=True, after=True)

                        run_embedded_job(
                            title="Restoring from health point",
                            argv=_urstack_command()
                            + ["--health-restore", "latest"],
                            pulsate=False,
                            success_toast="Restore point applied",
                            fail_toast="Restore finished with errors",
                            on_complete=after,
                            done_goes_home=False,
                            auto_complete=True,
                        )

                    try:
                        dialog = Adw.AlertDialog(
                            heading="Restore latest health point?",
                            body=(
                                "This rolls back UrStack Health config changes, "
                                "service and user-unit enablement, and — if the point "
                                "was taken for a package action — the DNF transaction. "
                                "Deleted caches, trash and logs are not recovered. "
                                "Continue?"
                            ),
                        )
                        dialog.add_response("cancel", "Cancel")
                        dialog.add_response("restore", "Restore")
                        dialog.set_response_appearance(
                            "restore", Adw.ResponseAppearance.DESTRUCTIVE
                        )
                        dialog.set_default_response("cancel")
                        dialog.set_close_response("cancel")

                        def on_resp(_d: Adw.AlertDialog, response: str) -> None:
                            if response == "restore":
                                confirmed()

                        dialog.connect("response", on_resp)
                        dialog.present(parent_win)
                        return
                    except Exception:  # noqa: BLE001
                        confirmed()

            content = build_health_content(
                _health_status_path(),
                parent_win=win,
                on_apply=do_apply,
                on_refresh=do_refresh,
                on_create_restore_point=do_create_rp,
                on_restore_latest=do_restore_latest,
                scanning=scanning,
            )
            health_slot.append(content)

        health_ctl["remount"] = lambda: _mount_health_content(scanning=False)

        def run_health_scan(*, embedded: bool, after: bool) -> None:
            if scan_inflight["health"]:
                toast.add_toast(Adw.Toast(title="A scan is already running"))
                return
            hs = _health_status_path()
            Path(hs).parent.mkdir(parents=True, exist_ok=True)
            scan_inflight["health"] = True
            _sync_checking_flag()
            _mount_health_content(scanning=True)
            # Always reveal Health. After apply/restore the visible page is
            # job-progress while session["nav"] is still "health", so a
            # "already on health" check would leave the user stuck.
            push_page("Health", health_slot, "Workstation diagnostics", tag="health")
            set_active("health")
            try:
                rebuild_overview()
            except Exception:  # noqa: BLE001
                pass

            def work() -> None:
                def done() -> bool:
                    scan_inflight["health"] = False
                    _sync_checking_flag()
                    _mount_health_content(scanning=False)
                    try:
                        rebuild_overview()
                    except Exception:  # noqa: BLE001
                        pass
                    toast.add_toast(Adw.Toast(title="Health scan complete"))
                    return False

                try:
                    run_health_scan_subprocess(hs)
                finally:
                    GLib.idle_add(done)

            threading.Thread(target=work, daemon=True).start()

        def push_health(*_a: object) -> bool:
            hs = Path(_health_status_path())
            if hs.is_file() and hs.stat().st_size > 0:
                _mount_health_content(scanning=False)
                push_page("Health", health_slot, "Workstation diagnostics", tag="health")
            else:
                run_health_scan(embedded=False, after=False)
            return False

        def push_look(*_a: object) -> bool:
            content = build_look_content(
                parent_win=win,
                on_export=start_look_export,
                on_install=start_look_install,
                on_store_install=start_look_store_install,
            )
            push_page("Look", content, "Theme pack and install", tag="look")
            return False

        def push_backup(*_a: object) -> bool:
            content = build_backup_restore_content(
                "backup",
                parent_win=win,
                on_start=start_backup_or_restore,
                on_back=None,
            )
            push_page("Backup", content, "Save a setup rebuild", tag="backup")
            return False

        def push_restore(*_a: object) -> bool:
            content = build_backup_restore_content(
                "restore",
                parent_win=win,
                on_start=start_backup_or_restore,
                on_back=None,
            )
            push_page("Restore", content, "Rebuild from a backup", tag="restore")
            return False

        def push_settings(*_a: object) -> bool:
            content = build_settings_content(
                args.config_file,
                on_rescan=None,  # scan in-place; keep main window open
                on_saved=None,
                toast_overlay=toast,
            )
            push_page("Settings", content, "Sources & behaviour", tag="settings")
            return False

        def push_log(*_a: object) -> bool:
            log_path = Path(args.log_file).expanduser()
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            outer.set_hexpand(True)
            outer.set_vexpand(True)

            scrolled_log, _clamp, list_col = page_scroll_body(spacing=12)
            raw = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.is_file()
                else ""
            ).strip()
            chunks = []
            if raw:
                chunks = re.split(r"(?m)(?=^\[[0-9T:\-+.]+\]\s*$)", raw)
                chunks = [c.strip() for c in chunks if c.strip()]

            chrome = page_chrome_box()
            chrome.append(
                page_hero(
                    str(len(chunks)) if chunks else "0",
                    "entries",
                    "Update history",
                    "Expand an entry to read what ran. Newest first.",
                    warn=False,
                    heading="History",
                    heading_sub="Chronological update log for this workstation",
                    icon_name=page_icon("log"),
                )
            )
            outer.append(chrome)
            list_col.append(page_callout("Log file", str(log_path)))
            list_col.append(page_section_label("Log entries"))

            if not raw:
                empty = Adw.StatusPage(
                    title="No history yet",
                    description="Apply updates once and entries will show up here.",
                    icon_name=page_icon("log"),
                )
                empty.add_css_class("compact")
                list_col.append(empty)
            else:
                listbox = Gtk.ListBox()
                listbox.add_css_class("boxed-list")
                listbox.set_selection_mode(Gtk.SelectionMode.NONE)
                for chunk in reversed(chunks[-80:]):
                    lines = chunk.splitlines()
                    head = lines[0] if lines else "Entry"
                    title = head.strip("[]") if head.startswith("[") else head[:80]
                    body = "\n".join(lines[1:]).strip() if len(lines) > 1 else chunk
                    row = Adw.ExpanderRow(
                        title=title[:120],
                        subtitle=(body.splitlines()[0][:100] if body else ""),
                    )
                    row.set_expanded(False)
                    info = Adw.ActionRow(
                        title="Details",
                        subtitle=body[:2000] if body else chunk[:2000],
                    )
                    try:
                        info.set_subtitle_lines(12)
                    except AttributeError:
                        pass
                    row.add_row(info)
                    listbox.append(row)
                list_col.append(listbox)

            outer.append(scrolled_log)
            push_page("History", outer, "Update log", tag="history")
            return False

        def push_runs(*_a: object) -> bool:
            runs_dir = Path(args.runs_dir).expanduser()
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            outer.set_hexpand(True)
            outer.set_vexpand(True)
            runs_preview = (
                sorted([p for p in runs_dir.iterdir() if p.is_dir()], reverse=True)
                if runs_dir.is_dir()
                else []
            )
            chrome = page_chrome_box()
            chrome.append(
                page_hero(
                    str(len(runs_preview)),
                    "sessions",
                    "Apply run logs",
                    "Each Apply creates a folder with stdout and a short summary.",
                    heading="Runs",
                    heading_sub="Per-session logs from Apply — pick a run to preview its summary",
                    icon_name=page_icon("runs"),
                )
            )
            outer.append(chrome)
            callout = page_callout("Runs directory", str(runs_dir))
            callout.set_margin_start(PAGE_SIDE_PAD)
            callout.set_margin_end(PAGE_SIDE_PAD)
            outer.append(callout)

            body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            body.set_margin_start(PAGE_SIDE_PAD)
            body.set_margin_end(PAGE_SIDE_PAD)
            body.set_margin_bottom(12)
            body.set_vexpand(True)
            body.set_hexpand(True)

            left_scroll = Gtk.ScrolledWindow()
            left_scroll.set_vexpand(True)
            left_scroll.set_hexpand(True)
            left_scroll.set_size_request(280, -1)
            left_clamp = Adw.Clamp(maximum_size=520)
            left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            left_title = Gtk.Label(label="Sessions", xalign=0.0)
            left_title.add_css_class("fu-section-title")
            left_col.append(left_title)
            listbox = Gtk.ListBox()
            listbox.add_css_class("boxed-list")
            listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            left_col.append(listbox)
            left_clamp.set_child(left_col)
            left_scroll.set_child(left_clamp)

            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            right.set_hexpand(True)
            right.set_vexpand(True)
            right_title = Gtk.Label(label="Summary", xalign=0.0)
            right_title.add_css_class("fu-section-title")
            right.append(right_title)
            detail_scroll = Gtk.ScrolledWindow()
            detail_scroll.set_vexpand(True)
            detail_scroll.set_hexpand(True)
            detail_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            detail_card.add_css_class("card")
            detail_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            detail_inner.set_margin_top(12)
            detail_inner.set_margin_bottom(12)
            detail_inner.set_margin_start(14)
            detail_inner.set_margin_end(14)
            detail = Gtk.Label(
                label="Select a run on the left to preview its summary.",
                xalign=0.0,
                wrap=True,
                selectable=True,
            )
            detail.add_css_class("fu-mono")
            detail_inner.append(detail)
            detail_card.append(detail_inner)
            detail_scroll.set_child(detail_card)
            right.append(detail_scroll)

            run_dirs: list[Path] = []
            if runs_dir.is_dir():
                run_dirs = sorted(
                    [p for p in runs_dir.iterdir() if p.is_dir()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )[:40]

            if not run_dirs:
                empty = Adw.StatusPage(
                    title="No runs yet",
                    description="After you apply updates, session folders appear here.",
                    icon_name=page_icon("runs"),
                )
                empty.add_css_class("compact")
                left_col.append(empty)
            else:
                for rd in run_dirs:
                    summary = rd / "summary.txt"
                    subtitle = "Has summary" if summary.is_file() else "No summary.txt"
                    row = Adw.ActionRow(title=rd.name, subtitle=subtitle)
                    row.set_activatable(True)
                    icon = Gtk.Image.new_from_icon_name(page_icon("runs"))
                    icon.set_pixel_size(18)
                    row.add_prefix(icon)
                    listbox.append(row)

                def show_run(idx: int) -> None:
                    if idx < 0 or idx >= len(run_dirs):
                        return
                    rd = run_dirs[idx]
                    summary = rd / "summary.txt"
                    if not summary.is_file():
                        candidates = list(rd.glob("*.txt")) + list(rd.glob("*.log"))
                        summary = candidates[0] if candidates else summary
                    if summary.is_file():
                        detail.set_text(
                            summary.read_text(encoding="utf-8", errors="replace")[:16000]
                        )
                    else:
                        detail.set_text(f"No summary in {rd}")

                def on_row(_lb: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
                    show_run(row.get_index())

                def on_sel(_lb: Gtk.ListBox, _row: Gtk.ListBoxRow | None = None) -> None:
                    row = listbox.get_selected_row()
                    if row is not None:
                        show_run(row.get_index())

                listbox.connect("row-activated", on_row)
                listbox.connect("row-selected", on_sel)
                # Prefetch newest
                listbox.select_row(listbox.get_row_at_index(0))
                show_run(0)

            body.append(left_scroll)
            body.append(right)
            outer.append(body)
            push_page("Runs", outer, "Apply session logs", tag="runs")
            return False

        nav_handlers["overview"] = go_overview
        nav_handlers["home"] = push_updates
        nav_handlers["apply"] = push_apply
        nav_handlers["apps"] = push_apps
        nav_handlers["health"] = push_health
        nav_handlers["look"] = push_look
        nav_handlers["backup"] = push_backup
        nav_handlers["restore"] = push_restore
        nav_handlers["settings"] = push_settings
        nav_handlers["log"] = push_log
        nav_handlers["runs"] = push_runs

        start = (getattr(args, "start_page", "") or "").strip().lower()
        if start in {"overview", "home", ""}:
            pass
        elif start == "updates":
            GLib.idle_add(push_updates)
        elif start == "apps":
            GLib.idle_add(push_apps)
        elif start == "health":
            GLib.idle_add(push_health)
        elif start == "look":
            GLib.idle_add(push_look)
        elif start == "settings":
            GLib.idle_add(push_settings)
        elif start == "backup":
            GLib.idle_add(push_backup)
        elif start == "restore":
            GLib.idle_add(push_restore)
        elif start == "home":
            GLib.idle_add(push_updates)

        def _on_backup_size_ready() -> bool:
            if not closing["confirmed"] and session.get("nav") == "overview":
                rebuild_overview()
            return False

        _BACKUP_SIZE_READY.clear()
        _BACKUP_SIZE_READY.append(_on_backup_size_ready)

        def _present_window() -> None:
            try:
                win.present()
            except Exception:  # noqa: BLE001
                pass

        def on_tray_page(_act: Gio.SimpleAction, param: GLib.Variant | None) -> None:
            page = (param.get_string() if param is not None else "").strip().lower()
            _present_window()
            if page in {"", "overview"}:
                go_overview()
                return
            if page in {"updates", "home"}:
                push_updates()
                return
            handler = nav_handlers.get(page)
            if callable(handler):
                handler()

        def on_tray_check(_act: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
            _present_window()
            start_overview_refresh()

        def on_tray_quit(_act: Gio.SimpleAction, _param: GLib.Variant | None) -> None:
            finish("close")

        page_act = Gio.SimpleAction.new("open-page", GLib.VariantType("s"))
        page_act.connect("activate", on_tray_page)
        app.add_action(page_act)
        check_act = Gio.SimpleAction.new("check", None)
        check_act.connect("activate", on_tray_check)
        app.add_action(check_act)
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", on_tray_quit)
        app.add_action(quit_act)

        def on_close(*_a: object) -> bool:
            if not closing["confirmed"]:
                if job_busy["v"]:
                    prompt_close()
                    return True
                closing["confirmed"] = True
            print(result["action"], flush=True)
            app.quit()
            return False

        win.connect("close-request", on_close)
        win.maximize()
        win.present()

    return run_app(URSTACK_APP_ID, build)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fedora-ui.py")
    sub = p.add_subparsers(dest="mode", required=True)

    hub = sub.add_parser("hub")
    hub.add_argument("--file", required=True)
    hub.add_argument("--has-updates", type=int, default=0)
    hub.add_argument("--enable-backup", type=int, default=0)
    hub.add_argument("--title", default="UrStack")

    chk = sub.add_parser("checklist")
    chk.add_argument("--items-file", required=True)
    chk.add_argument("--title", default="Select")
    chk.add_argument("--text", default="")
    chk.add_argument("--ok-label", default="Continue")

    rad = sub.add_parser("radio")
    rad.add_argument("--items-file", required=True)
    rad.add_argument("--title", default="Choose")
    rad.add_argument("--text", default="")
    rad.add_argument("--ok-label", default="Continue")

    txt = sub.add_parser("text")
    txt.add_argument("--file")
    txt.add_argument("--text")
    txt.add_argument("--title", default="Details")
    txt.add_argument("--ok-label", default="Back")

    msg = sub.add_parser("message")
    msg.add_argument("--type", choices=("info", "error"), default="info")
    msg.add_argument("--text", required=True)
    msg.add_argument("--title", default="UrStack")

    ask = sub.add_parser("ask")
    ask.add_argument("--text", required=True)
    ask.add_argument("--title", default="Confirm")

    folder = sub.add_parser("folder")
    folder.add_argument("--title", default="Choose folder")
    folder.add_argument("--start", default=os.path.expanduser("~"))

    prog = sub.add_parser("progress")
    prog.add_argument("--title", default="Applying updates")
    prog.add_argument("--cancel-flag", default="")
    prog.add_argument("--auto-close", action="store_true", default=True)
    prog.add_argument("--no-auto-close", action="store_false", dest="auto_close")
    prog.add_argument("--pulsate", action="store_true", default=False)
    prog.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="Status + bar only (no empty log panel). Implied by --pulsate.",
    )
    prog.add_argument(
        "--no-cancel",
        action="store_true",
        default=False,
        help="Hide the Cancel button (used for non-cancellable checks).",
    )

    runs = sub.add_parser("runs")
    runs.add_argument("--title", default="Update run logs")
    runs.add_argument(
        "--runs-dir",
        default=str(Path.home() / ".local/state/urstack/runs"),
    )

    settings = sub.add_parser("settings")
    settings.add_argument("--title", default="UrStack settings")
    settings.add_argument(
        "--config-file",
        default=str(Path.home() / ".config/urstack/config.conf"),
    )

    catalog = sub.add_parser("catalog")
    catalog.add_argument("--title", default="UrStack — Apps")
    catalog.add_argument("--status-file", required=True)
    catalog.add_argument("--category", default="")

    shell = sub.add_parser("shell")
    shell.add_argument("--file", required=True)
    shell.add_argument("--has-updates", type=int, default=0)
    shell.add_argument("--enable-backup", type=int, default=0)
    shell.add_argument("--title", default="UrStack")
    shell.add_argument("--status-file", required=True)
    shell.add_argument("--config-file", default=str(Path.home() / ".config/urstack/config.conf"))
    shell.add_argument("--log-file", default=str(Path.home() / ".local/state/urstack/urstack.log"))
    shell.add_argument(
        "--runs-dir",
        default=str(Path.home() / ".local/state/urstack/runs"),
    )
    shell.add_argument("--category", default="")
    shell.add_argument(
        "--start-page",
        default="",
        help="Optional: overview|updates|apps|health|look|settings|backup|restore",
    )
    shell.add_argument(
        "--sections-file",
        default="",
        help="Checklist items for in-app Apply updates",
    )
    shell.add_argument(
        "--check-dir",
        default="",
        help="Session check directory for in-app apply",
    )
    shell.add_argument(
        "--pending-check",
        type=int,
        default=0,
        help="Run the initial update check inside the shell window",
    )

    return p


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] not in {
        "hub", "checklist", "radio", "text", "message", "ask", "folder",
        "progress", "runs", "settings", "catalog", "shell", "-h", "--help",
    } and argv[0].startswith("-"):
        argv = ["hub", *argv]

    args = build_parser().parse_args(argv)
    modes = {
        "hub": mode_hub,
        "checklist": mode_checklist,
        "radio": mode_radio,
        "text": mode_text,
        "message": mode_message,
        "ask": mode_ask,
        "folder": mode_folder,
        "progress": mode_progress,
        "runs": mode_runs,
        "settings": mode_settings,
        "catalog": mode_catalog,
        "shell": mode_shell,
    }
    return modes[args.mode](args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        print(f"fedora-ui error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
