#!/usr/bin/env python3
"""Silent-check indicator: StatusNotifierItem (KDE/XFCE/Cinnamon/COSMIC) or a
small GTK window that shows on the GNOME dash / any taskbar that has no tray.

Commands on --fifo (one per line): checking | updates | idle | quit
Left-click / Open launches the main UrStack window and exits.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_NAME = "UrStack"
ITEM_PATH = "/StatusNotifierItem"
SNI_IFACE = "org.kde.StatusNotifierItem"
WATCHERS = (
    "org.kde.StatusNotifierWatcher",
    "org.freedesktop.StatusNotifierWatcher",
)

SNI_XML = f"""
<node>
  <interface name="{SNI_IFACE}">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="OverlayIconPixmap" type="a(iiay)" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="AttentionIconPixmap" type="a(iiay)" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""


def icon_pixmap_from_png(path: str) -> list[tuple[int, int, bytes]]:
    """SNI IconPixmap: ARGB32 in network (big-endian) byte order."""
    if not path or not Path(path).is_file():
        return []
    try:
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ValueError, ImportError):
        return []
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(path, 48, 48)
    except GLib.Error:
        return []
    if pb is None:
        return []
    w, h = pb.get_width(), pb.get_height()
    nch = pb.get_n_channels()
    stride = pb.get_rowstride()
    src = bytes(pb.get_pixels())
    out = bytearray(w * h * 4)
    o = 0
    for y in range(h):
        row = y * stride
        for x in range(w):
            i = row + x * nch
            r, g, b = src[i], src[i + 1], src[i + 2]
            a = src[i + 3] if nch >= 4 else 255
            out[o : o + 4] = bytes((a, r, g, b))
            o += 4
    return [(w, h, bytes(out))]


def _empty_pixmaps() -> GLib.Variant:
    return GLib.Variant("a(iiay)", [])


def _pixmaps_variant(pix: list[tuple[int, int, bytes]]) -> GLib.Variant:
    return GLib.Variant("a(iiay)", [(w, h, p) for w, h, p in pix])


class SilentIndicator:
    def __init__(self, *, icon: str, open_cmd: list[str], pixmaps: list) -> None:
        self.icon = icon
        self.open_cmd = open_cmd
        self.pixmaps = pixmaps
        self.status = "Active"
        self.title = APP_NAME
        self.body = "Checking for updates…"
        self.loop = GLib.MainLoop()
        self.conn: Gio.DBusConnection | None = None
        self._win = None
        self._gtk_app = None

    def quit(self) -> None:
        try:
            self.loop.quit()
        except Exception:  # noqa: BLE001
            pass

    def open_app(self) -> None:
        if self.open_cmd:
            try:
                os.spawnvp(os.P_NOWAIT, self.open_cmd[0], self.open_cmd)
            except OSError:
                pass
        self.quit()

    def set_mode(self, mode: str) -> None:
        mode = (mode or "").strip().lower()
        if mode == "quit":
            self.quit()
            return
        if mode == "checking":
            self.status = "Active"
            self.body = "Checking for updates…"
        elif mode == "updates":
            self.status = "NeedsAttention"
            self.body = "Updates available — click to open"
        elif mode == "idle":
            self.status = "Active"
            self.body = "You're up to date"
        else:
            return
        self._emit_sni()
        self._sync_window()

    def _tooltip_variant(self) -> GLib.Variant:
        return GLib.Variant.new_tuple(
            GLib.Variant("s", "urstack"),
            _pixmaps_variant(self.pixmaps),
            GLib.Variant("s", self.title),
            GLib.Variant("s", self.body),
        )

    def _prop(self, name: str) -> GLib.Variant | None:
        mapping = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "urstack"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", self.status),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "urstack"),
            "IconPixmap": _pixmaps_variant(self.pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": _empty_pixmaps(),
            "AttentionIconName": GLib.Variant("s", "urstack"),
            "AttentionIconPixmap": _pixmaps_variant(self.pixmaps),
            "ToolTip": self._tooltip_variant(),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/NO_DBUSMENU"),
        }
        return mapping.get(name)

    def _emit_sni(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(None, ITEM_PATH, SNI_IFACE, "NewStatus", GLib.Variant("(s)", (self.status,)))
            self.conn.emit_signal(None, ITEM_PATH, SNI_IFACE, "NewToolTip", None)
            self.conn.emit_signal(None, ITEM_PATH, SNI_IFACE, "NewIcon", None)
        except GLib.Error:
            pass

    def start_sni(self) -> bool:
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return False
        service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        try:
            Gio.bus_own_name_on_connection(
                conn,
                service,
                Gio.BusNameOwnerFlags.NONE,
                None,
                None,
            )
        except GLib.Error:
            pass
        node = Gio.DBusNodeInfo.new_for_xml(SNI_XML)
        iface = node.interfaces[0]

        def on_method(_c, _s, _p, _i, method, _params, invocation) -> None:
            if method in {"Activate", "ContextMenu", "SecondaryActivate"}:
                invocation.return_value(None)
                GLib.idle_add(self.open_app)
            else:
                invocation.return_value(None)

        def on_get(_c, _s, _p, _i, name):
            return self._prop(name)

        try:
            conn.register_object(ITEM_PATH, iface, on_method, on_get, None)
        except GLib.Error:
            return False
        self.conn = conn
        unique = conn.get_unique_name() or ""
        args = (service, ITEM_PATH, f"{unique}{ITEM_PATH}")
        for dest in WATCHERS:
            for arg in args:
                try:
                    conn.call_sync(
                        dest,
                        "/StatusNotifierWatcher",
                        dest,
                        "RegisterStatusNotifierItem",
                        GLib.Variant("(s)", (arg,)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        2500,
                        None,
                    )
                    return True
                except GLib.Error:
                    continue
        self.conn = None
        return False

    def start_window(self) -> bool:
        try:
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gdk, Gtk
        except (ValueError, ImportError):
            return False
        GLib.set_prgname("urstack")
        GLib.set_application_name(APP_NAME)
        try:
            Gdk.set_program_class("urstack")
        except Exception:  # noqa: BLE001
            pass

        def build(app: Gtk.Application) -> None:
            win = Gtk.ApplicationWindow(application=app, title=APP_NAME)
            win.set_default_size(340, 128)
            win.set_resizable(False)
            try:
                win.set_icon_name("urstack")
            except Exception:  # noqa: BLE001
                pass
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
            box.set_margin_top(16)
            box.set_margin_bottom(16)
            box.set_margin_start(18)
            box.set_margin_end(18)
            head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            if self.icon and Path(self.icon).is_file():
                img = Gtk.Image.new_from_file(self.icon)
                img.set_pixel_size(48)
                head.append(img)
            lab = Gtk.Label(label=self.body, wrap=True, xalign=0.0)
            lab.set_hexpand(True)
            lab.set_wrap(True)
            head.append(lab)
            box.append(head)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_halign(Gtk.Align.END)
            open_btn = Gtk.Button(label="Open UrStack")
            open_btn.add_css_class("suggested-action")
            open_btn.connect("clicked", lambda *_: self.open_app())
            row.append(open_btn)
            box.append(row)
            win.set_child(box)
            def on_close(*_a: object) -> bool:
                self.quit()
                return False

            win.connect("close-request", on_close)
            self._win = (win, lab)
            win.present()
            # Stay on the dash/taskbar without covering the desktop.
            GLib.idle_add(win.minimize)

        app = Gtk.Application(
            application_id="com.local.urstack.tray",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        app.connect("activate", build)
        try:
            if not app.register(None):
                return False
        except GLib.Error:
            return False
        app.activate()
        self._gtk_app = app
        return self._win is not None

    def _sync_window(self) -> None:
        pair = self._win
        if not pair:
            return
        _win, lab = pair
        try:
            lab.set_label(self.body)
            _win.set_title(f"{APP_NAME} — {self.body}")
        except Exception:  # noqa: BLE001
            pass


def read_fifo(path: str, indicator: SilentIndicator) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                GLib.idle_add(indicator.set_mode, line)
                if line == "quit":
                    return
    except OSError:
        GLib.idle_add(indicator.quit)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="urstack-tray")
    p.add_argument("--fifo", required=True)
    p.add_argument("--icon", default="")
    p.add_argument("--open-cmd", default="urstack")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    open_cmd = [c for c in str(args.open_cmd).split() if c] or ["urstack"]
    pix = icon_pixmap_from_png(args.icon)
    ind = SilentIndicator(icon=args.icon, open_cmd=open_cmd, pixmaps=pix)
    if not ind.start_sni():
        if not ind.start_window():
            return 0
    threading.Thread(target=read_fifo, args=(args.fifo, ind), daemon=True).start()
    try:
        ind.loop.run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
