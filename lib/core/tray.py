#!/usr/bin/env python3
"""UrStack tray indicator: StatusNotifierItem (KDE/XFCE/Cinnamon/COSMIC) or a
small GTK window that shows on the GNOME dash / any taskbar that has no tray.

The indicator outlives the run that spawned it: it stays until "Quit UrStack" is
chosen from its menu, so only one may exist at a time. That is enforced by owning
a well-known bus name — a second instance exits immediately rather than stacking
a duplicate icon in the panel.

Commands on --fifo (one per line): checking | updates | idle | quit
Left-click / Open raises a running UrStack window, or launches one; right-click
opens the menu (pages, check, quit). The artwork is the grey tray variant of the
app icon, so it sits with the other monochrome indicators.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

APP_NAME = "UrStack"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/StatusNotifierItem/Menu"
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
# Owning this name is what makes the tray a singleton.
SINGLETON_NAME = "com.local.urstack.Tray"
WATCHERS = (
    "org.kde.StatusNotifierWatcher",
    "org.freedesktop.StatusNotifierWatcher",
)
# Tray icons sit among monochrome system indicators, so UrStack uses the grey
# variant of its logo rather than the colour one.
TRAY_ICON_NAME = "urstack-tray"
# Same id the GTK shell claims, so the tray can raise/navigate a running window
# instead of spawning a second process.
APP_BUS_NAME = "com.local.urstack"
APP_OBJECT_PATH = "/com/local/urstack"

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

MENU_XML = f"""
<node>
  <interface name="{MENU_IFACE}">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{{sv}}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{{sv}})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="EventGroup">
      <arg name="events" type="a(isvu)" direction="in"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{{sv}})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
    <signal name="ItemActivationRequested">
      <arg name="id" type="i"/>
      <arg name="timestamp" type="u"/>
    </signal>
  </interface>
</node>
"""

# id, action key, label. A blank action is a separator; "status" is the live
# summary line at the top and is never clickable.
MENU_ITEMS: tuple[tuple[int, str, str], ...] = (
    (1, "status", ""),
    (2, "", ""),
    (3, "open", "Open UrStack"),
    (4, "check", "Check for updates"),
    (5, "", ""),
    (6, "updates", "Updates"),
    (7, "apps", "Apps"),
    (8, "health", "Health"),
    (9, "backup", "Backup"),
    (10, "restore", "Restore"),
    (11, "settings", "Settings"),
    (12, "", ""),
    (13, "quit", "Quit UrStack"),
)
PAGE_ACTIONS = frozenset({"updates", "apps", "health", "backup", "restore", "settings"})


def default_fifo_path() -> str:
    """Stable path so any urstack run can talk to an already-running tray."""
    base = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return str(Path(base) / "urstack-tray.fifo")


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


def _session_bus() -> Gio.DBusConnection | None:
    try:
        return Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error:
        return None


def app_is_running(conn: Gio.DBusConnection | None = None) -> bool:
    """True when the main UrStack window owns com.local.urstack on the session bus."""
    own = conn or _session_bus()
    if own is None:
        return False
    try:
        reply = own.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "NameHasOwner",
            GLib.Variant("(s)", (APP_BUS_NAME,)),
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            800,
            None,
        )
        return bool(reply.unpack()[0])
    except GLib.Error:
        return False


def activate_running_app(*, page: str = "", action: str = "") -> bool:
    """Ask a running UrStack window to raise, open a page, check, or quit.

    Returns False when no window is on the bus so the caller can spawn one.
    """
    conn = _session_bus()
    if conn is None or not app_is_running(conn):
        return False
    try:
        if action:
            params = GLib.Variant.new_tuple(
                GLib.Variant("s", action),
                GLib.Variant("av", []),
                GLib.Variant("a{sv}", {}),
            )
            conn.call_sync(
                APP_BUS_NAME,
                APP_OBJECT_PATH,
                "org.freedesktop.Application",
                "ActivateAction",
                params,
                None,
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
            return True
        if page:
            params = GLib.Variant.new_tuple(
                GLib.Variant("s", "open-page"),
                GLib.Variant("av", [GLib.Variant("s", page)]),
                GLib.Variant("a{sv}", {}),
            )
            conn.call_sync(
                APP_BUS_NAME,
                APP_OBJECT_PATH,
                "org.freedesktop.Application",
                "ActivateAction",
                params,
                None,
                Gio.DBusCallFlags.NONE,
                1500,
                None,
            )
            return True
        conn.call_sync(
            APP_BUS_NAME,
            APP_OBJECT_PATH,
            "org.freedesktop.Application",
            "Activate",
            GLib.Variant("(a{sv})", ({},)),
            None,
            Gio.DBusCallFlags.NONE,
            1500,
            None,
        )
        return True
    except GLib.Error:
        return False


def claim_singleton(conn: Gio.DBusConnection) -> bool:
    """True if this process now owns the tray name, False if one already runs."""
    try:
        reply = conn.call_sync(
            "org.freedesktop.DBus",
            "/org/freedesktop/DBus",
            "org.freedesktop.DBus",
            "RequestName",
            # 4 = DBUS_NAME_FLAG_DO_NOT_QUEUE: fail fast instead of waiting for
            # the running tray to exit and then silently taking over later.
            GLib.Variant("(su)", (SINGLETON_NAME, 4)),
            GLib.VariantType("(u)"),
            Gio.DBusCallFlags.NONE,
            2500,
            None,
        )
    except GLib.Error:
        # No session bus policy for the name: fall through and run anyway rather
        # than leaving the user with no indicator at all.
        return True
    # 1 = PRIMARY_OWNER, 4 = ALREADY_OWNER
    return reply.unpack()[0] in (1, 4)


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
        self.revision = 1
        self._win = None
        self._gtk_app = None
        self._children: list[subprocess.Popen] = []

    # -- lifecycle ---------------------------------------------------------
    def quit(self) -> None:
        with contextlib.suppress(Exception):
            self.loop.quit()

    def _reap(self) -> None:
        """Detached launches still need collecting or they linger as zombies."""
        self._children = [p for p in self._children if p.poll() is None]

    def _spawn(self, args: list[str]) -> None:
        if not self.open_cmd:
            return
        self._reap()
        try:
            # start_new_session detaches the child so closing the tray later, or
            # the shell that spawned it, does not take the GUI down with it.
            self._children.append(
                subprocess.Popen(
                    [*self.open_cmd, *args],
                    start_new_session=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        except OSError:
            pass

    def open_app(self) -> None:
        """Left-click / Open. Raise the running window, or launch one."""
        if activate_running_app():
            return
        self._spawn([])

    def run_action(self, action: str) -> None:
        if action == "open":
            self.open_app()
        elif action == "check":
            self.set_mode("checking")
            if activate_running_app(action="check"):
                return
            self._spawn(["--check", "--tray"])
        elif action in PAGE_ACTIONS:
            if not activate_running_app(page=action):
                self._spawn(["--page", action])
        elif action == "quit":
            activate_running_app(action="quit")
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
        self._emit_menu_update()
        self._sync_window()

    # -- StatusNotifierItem ------------------------------------------------
    def _tooltip_variant(self) -> GLib.Variant:
        return GLib.Variant.new_tuple(
            GLib.Variant("s", TRAY_ICON_NAME),
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
            "IconName": GLib.Variant("s", TRAY_ICON_NAME),
            "IconPixmap": _pixmaps_variant(self.pixmaps),
            "OverlayIconName": GLib.Variant("s", ""),
            "OverlayIconPixmap": _empty_pixmaps(),
            # Same grey art when attention is requested: the panel highlights the
            # item itself, and a sudden colour icon would break tray convention.
            "AttentionIconName": GLib.Variant("s", TRAY_ICON_NAME),
            "AttentionIconPixmap": _pixmaps_variant(self.pixmaps),
            "ToolTip": self._tooltip_variant(),
            # False keeps left-click as Activate; the host uses Menu for right-click.
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return mapping.get(name)

    def _emit_sni(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.emit_signal(
                None, ITEM_PATH, SNI_IFACE, "NewStatus", GLib.Variant("(s)", (self.status,))
            )
            self.conn.emit_signal(None, ITEM_PATH, SNI_IFACE, "NewToolTip", None)
            self.conn.emit_signal(None, ITEM_PATH, SNI_IFACE, "NewIcon", None)
        except GLib.Error:
            pass

    # -- dbusmenu ----------------------------------------------------------
    def _item_props(self, item_id: int, action: str, label: str) -> dict[str, GLib.Variant]:
        if not action:
            return {"type": GLib.Variant("s", "separator")}
        if action == "status":
            return {
                "label": GLib.Variant("s", self.body),
                "enabled": GLib.Variant("b", False),
            }
        props = {
            "label": GLib.Variant("s", label),
            "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True),
        }
        if item_id == 3:
            props["icon-name"] = GLib.Variant("s", TRAY_ICON_NAME)
        return props

    def _layout_variant(self, parent: int, filter_props: list[str] | None) -> GLib.Variant:
        def props_for(item_id: int, action: str, label: str) -> dict[str, GLib.Variant]:
            props = self._item_props(item_id, action, label)
            if filter_props:
                props = {k: v for k, v in props.items() if k in filter_props}
            return props

        # A pre-built variant cannot be nested through a format string, so the
        # revision and the layout are joined with new_tuple instead.
        def reply(layout: GLib.Variant) -> GLib.Variant:
            return GLib.Variant.new_tuple(GLib.Variant("u", self.revision), layout)

        # The menu is flat, so anything other than the root is a leaf. Returning
        # the root's children for a leaf id would make hosts nest it into itself.
        if parent != 0:
            for item_id, action, label in MENU_ITEMS:
                if item_id == parent:
                    props = props_for(item_id, action, label)
                    return reply(GLib.Variant("(ia{sv}av)", (item_id, props, [])))
            return reply(GLib.Variant("(ia{sv}av)", (parent, {}, [])))

        children = [
            GLib.Variant("(ia{sv}av)", (item_id, props_for(item_id, action, label), []))
            for item_id, action, label in MENU_ITEMS
        ]
        root = GLib.Variant(
            "(ia{sv}av)",
            (0, {"children-display": GLib.Variant("s", "submenu")}, children),
        )
        return reply(root)

    def _emit_menu_update(self) -> None:
        """Refresh the status line without rebuilding the whole menu."""
        if self.conn is None:
            return
        updated = GLib.Variant(
            "a(ia{sv})", [(1, {"label": GLib.Variant("s", self.body)})]
        )
        removed = GLib.Variant("a(ias)", [])
        try:
            self.conn.emit_signal(
                None,
                MENU_PATH,
                MENU_IFACE,
                "ItemsPropertiesUpdated",
                GLib.Variant.new_tuple(updated, removed),
            )
        except GLib.Error:
            pass

    def _on_menu_method(self, _c, _s, _p, _i, method, params, invocation) -> None:
        args = params.unpack() if params else ()
        if method == "GetLayout":
            parent, _depth, names = args
            invocation.return_value(self._layout_variant(parent, list(names)))
        elif method == "GetGroupProperties":
            ids, names = args
            wanted = list(names)
            rows = []
            for item_id, action, label in MENU_ITEMS:
                if ids and item_id not in ids:
                    continue
                props = self._item_props(item_id, action, label)
                if wanted:
                    props = {k: v for k, v in props.items() if k in wanted}
                rows.append((item_id, props))
            invocation.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "GetProperty":
            item_id, name = args
            value = None
            for iid, action, label in MENU_ITEMS:
                if iid == item_id:
                    value = self._item_props(iid, action, label).get(name)
                    break
            # Out-arg is a variant, so it must be boxed: new_tuple would collapse
            # it to the inner type and the host would reject the reply.
            invocation.return_value(
                GLib.Variant("(v)", (value if value is not None else GLib.Variant("s", ""),))
            )
        elif method == "Event":
            item_id, event_id, _data, _ts = args
            if event_id == "clicked":
                for iid, action, _label in MENU_ITEMS:
                    if iid == item_id and action not in ("", "status"):
                        GLib.idle_add(self.run_action, action)
                        break
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _data, _ts in args[0]:
                if event_id != "clicked":
                    continue
                for iid, action, _label in MENU_ITEMS:
                    if iid == item_id and action not in ("", "status"):
                        GLib.idle_add(self.run_action, action)
                        break
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            # True asks the host to re-read the layout, which is how the status
            # line is guaranteed to be current every time the menu opens.
            invocation.return_value(GLib.Variant("(b)", (True,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_value(None)

    def _on_menu_get(self, _c, _s, _p, _i, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def start_sni(self) -> bool:
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return False
        if not claim_singleton(conn):
            return False
        service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        with contextlib.suppress(GLib.Error):
            Gio.bus_own_name_on_connection(
                conn,
                service,
                Gio.BusNameOwnerFlags.NONE,
                None,
                None,
            )
        item_iface = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu_iface = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]

        def on_method(_c, _s, _p, _i, method, _params, invocation) -> None:
            if method == "Activate":
                invocation.return_value(None)
                GLib.idle_add(self.open_app)
            else:
                # ContextMenu/SecondaryActivate: the host draws Menu itself, so
                # opening the window here would fight the menu it just showed.
                invocation.return_value(None)

        def on_get(_c, _s, _p, _i, name):
            return self._prop(name)

        try:
            conn.register_object(ITEM_PATH, item_iface, on_method, on_get, None)
            conn.register_object(MENU_PATH, menu_iface, self._on_menu_method, self._on_menu_get, None)
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

    # -- GTK fallback ------------------------------------------------------
    def start_window(self) -> bool:
        try:
            gi.require_version("Gtk", "4.0")
            from gi.repository import Gdk, Gtk
        except (ValueError, ImportError):
            return False
        GLib.set_prgname("urstack")
        GLib.set_application_name(APP_NAME)
        with contextlib.suppress(Exception):
            Gdk.set_program_class("urstack")

        def build(app: Gtk.Application) -> None:
            win = Gtk.ApplicationWindow(application=app, title=APP_NAME)
            win.set_default_size(360, 420)
            win.set_resizable(False)
            with contextlib.suppress(Exception):
                win.set_icon_name(TRAY_ICON_NAME)
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
            # Same destinations as the tray menu, for desktops with no tray at all.
            for action, label in (
                ("check", "Check for updates"),
                ("updates", "Updates"),
                ("apps", "Apps"),
                ("health", "Health"),
                ("backup", "Backup"),
                ("restore", "Restore"),
                ("settings", "Settings"),
            ):
                btn = Gtk.Button(label=label)
                btn.connect("clicked", lambda *_a, a=action: self.run_action(a))
                box.append(btn)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_halign(Gtk.Align.END)
            quit_btn = Gtk.Button(label="Quit")
            quit_btn.connect("clicked", lambda *_: self.quit())
            row.append(quit_btn)
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
    """Serve the command FIFO for as long as the tray lives.

    Reopened after every writer disconnects: the tray now outlives the run that
    started it, so a closed pipe means "that run finished", not "time to exit".
    """
    while True:
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
            return


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="urstack-tray")
    p.add_argument("--fifo", default="")
    p.add_argument("--icon", default="")
    p.add_argument("--open-cmd", default="urstack")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    fifo = args.fifo or default_fifo_path()
    open_cmd = [c for c in str(args.open_cmd).split() if c] or ["urstack"]
    pix = icon_pixmap_from_png(args.icon)
    ind = SilentIndicator(icon=args.icon, open_cmd=open_cmd, pixmaps=pix)
    if not ind.start_sni():
        # start_sni also returns False when another tray owns the name, in which
        # case a fallback window would be the duplicate we are trying to avoid.
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            taken = not claim_singleton(conn)
        except GLib.Error:
            taken = False
        if taken or not ind.start_window():
            return 0
    if not Path(fifo).exists():
        with contextlib.suppress(OSError):
            os.mkfifo(fifo, 0o600)
    threading.Thread(target=read_fifo, args=(fifo, ind), daemon=True).start()
    try:
        ind.loop.run()
    except KeyboardInterrupt:
        pass
    with contextlib.suppress(OSError):
        os.unlink(fifo)
    return 0


if __name__ == "__main__":
    sys.exit(main())
