#!/usr/bin/env python3
"""Look packs: export the live theme, install archives, refuse path tricks."""

from __future__ import annotations

import importlib.util
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOOK_PY = ROOT / "lib" / "core" / "look.py"


def load_look():
    import sys

    existing = sys.modules.get("urstack_look")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("urstack_look", LOOK_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["urstack_look"] = mod
    spec.loader.exec_module(mod)
    return mod


class LookTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.look = load_look()

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.home = Path(self._td.name) / "home"
        self.home.mkdir()
        self.out = Path(self._td.name) / "out"
        self.out.mkdir()

    def tearDown(self) -> None:
        self._td.cleanup()

    def _plasma_home(self) -> Path:
        cfg = self.home / ".config"
        cfg.mkdir()
        (cfg / "kdeglobals").write_text(
            "[Icons]\nTheme=MyPapirus\n\n[General]\nColorScheme=MyScheme\n"
            "[KDE]\nLookAndFeelPackage=org.kde.breeze.desktop\n",
            encoding="utf-8",
        )
        (cfg / "kcminputrc").write_text("[Mouse]\ncursorTheme=MyCursors\n", encoding="utf-8")
        applets = cfg / "plasma-org.kde.plasma.desktop-appletsrc"
        wall = self.home / "Pictures"
        wall.mkdir()
        pic = wall / "forest.png"
        pic.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        applets.write_text(
            "[Containments][1][Wallpaper][org.kde.image][General]\n"
            f"Image={pic}\n"
            "plugin=org.kde.image\n\n"
            "[Containments][1][Applets][2]\n"
            "plugin=com.example.customclock\n",
            encoding="utf-8",
        )
        icons = self.home / ".local/share/icons/MyPapirus"
        icons.mkdir(parents=True)
        (icons / "index.theme").write_text("[Icon Theme]\nName=MyPapirus\n", encoding="utf-8")
        (icons / "16x16").mkdir()
        (icons / "16x16" / "place.png").write_bytes(b"icon")
        cursors = self.home / ".local/share/icons/MyCursors"
        cursors.mkdir(parents=True)
        (cursors / "index.theme").write_text("[Icon Theme]\nName=MyCursors\n", encoding="utf-8")
        (cursors / "cursors").mkdir()
        (cursors / "cursors" / "left_ptr").write_bytes(b"cur")
        plasmoid = self.home / ".local/share/plasma/plasmoids/com.example.customclock"
        plasmoid.mkdir(parents=True)
        (plasmoid / "metadata.json").write_text(
            '{"KPackageStructure":"Plasma/Applet"}\n', encoding="utf-8"
        )
        colors = self.home / ".local/share/color-schemes"
        colors.mkdir(parents=True)
        (colors / "MyScheme.colors").write_text("[General]\nName=MyScheme\n", encoding="utf-8")
        gtk = cfg / "gtk-3.0"
        gtk.mkdir()
        (gtk / "settings.ini").write_text(
            "[Settings]\ngtk-theme-name=Adwaita-dark\n", encoding="utf-8"
        )
        return pic

    def test_detect_plasma_and_gnome(self) -> None:
        d = self.look.detect_desktop
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "KDE"}), "plasma")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "GNOME"}), "gnome")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "X-Cinnamon"}), "cinnamon")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "XFCE"}), "xfce")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "COSMIC"}), "cosmic")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "MATE"}), "mate")
        self.assertEqual(d({"XDG_CURRENT_DESKTOP": "LXQt"}), "lxqt")
        self.assertEqual(d({}), "unknown")

    def test_inspect_plasma_look(self) -> None:
        pic = self._plasma_home()
        snap = self.look.inspect_look(
            self.home, {"XDG_CURRENT_DESKTOP": "KDE"}
        )
        self.assertEqual(snap.desktop, "plasma")
        self.assertEqual(snap.icon_name, "MyPapirus")
        self.assertEqual(snap.cursor_name, "MyCursors")
        self.assertEqual(snap.color_scheme, "MyScheme")
        self.assertIn(pic, snap.wallpaper_files)
        ids = {it.id: it for it in snap.items}
        self.assertTrue(ids["icons"].bundled)
        self.assertTrue(ids["wallpaper"].bundled)
        self.assertIn("com.example.customclock", snap.widget_ids)

    def test_export_and_install_roundtrip(self) -> None:
        self._plasma_home()
        dest = self.out / "look.tar.xz"
        packed = self.look.export_look(
            dest,
            home=self.home,
            environ={"XDG_CURRENT_DESKTOP": "KDE"},
            name="Test look",
        )
        self.assertTrue(packed.is_file())
        info = self.look.inspect_archive(packed)
        self.assertEqual(info.kind, "urstack-look")
        self.assertEqual(info.name, "Test look")
        self.assertIn("wallpaper", info.items)
        self.assertIn("icons", info.items)

        other = Path(self._td.name) / "other"
        other.mkdir()
        result = self.look.install_archive(
            packed, home=other, apply=False, environ={"XDG_CURRENT_DESKTOP": "KDE"}
        )
        self.assertTrue(result["ok"])
        self.assertTrue(
            (other / ".local/share/icons/MyPapirus/index.theme").is_file()
        )
        self.assertTrue(
            (other / ".local/share/wallpapers/urstack/forest.png").is_file()
        )
        self.assertTrue(
            (
                other
                / ".local/share/plasma/plasmoids/com.example.customclock/metadata.json"
            ).is_file()
        )
        kdeglobals = (other / ".config/kdeglobals").read_text(encoding="utf-8")
        self.assertNotIn("__URSTACK_HOME__", kdeglobals)
        self.assertIn("MyPapirus", kdeglobals)

    def test_refuses_path_traversal_tar(self) -> None:
        evil = self.out / "evil.tar"
        with tarfile.open(evil, "w") as tf:
            info = tarfile.TarInfo("../../tmp/pwned")
            data = b"nope"
            info.size = len(data)
            tf.addfile(info, fileobj=__import__("io").BytesIO(data))
        with self.assertRaises(self.look.LookError):
            self.look.install_archive(evil, home=self.home, apply=False)

    def test_refuses_absolute_zip_member(self) -> None:
        evil = self.out / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("/tmp/pwned", "nope")
        info = self.look.inspect_archive(evil)
        self.assertTrue(info.unsafe)
        with self.assertRaises(self.look.LookError):
            self.look.install_archive(evil, home=self.home, apply=False)

    def test_skips_symlink_members(self) -> None:
        evil = self.out / "link.tar"
        with tarfile.open(evil, "w") as tf:
            info = tarfile.TarInfo("escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
            safe = tarfile.TarInfo("readme.txt")
            data = b"ok"
            safe.size = len(data)
            tf.addfile(safe, fileobj=__import__("io").BytesIO(data))
        dest = Path(self._td.name) / "extracted"
        self.look._extract_all(evil, dest)
        self.assertTrue((dest / "readme.txt").is_file())
        self.assertFalse((dest / "escape").exists() or (dest / "escape").is_symlink())

    def test_third_party_icon_theme_zip(self) -> None:
        zpath = self.out / "PrettyIcons.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr("PrettyIcons/index.theme", "[Icon Theme]\nName=PrettyIcons\n")
            zf.writestr("PrettyIcons/16x16/apps/foo.png", b"icon")
        info = self.look.inspect_archive(zpath)
        self.assertEqual(info.kind, "icons")
        result = self.look.install_archive(zpath, home=self.home, apply=False)
        self.assertTrue(
            (self.home / ".local/share/icons/PrettyIcons/index.theme").is_file()
        )
        self.assertIn("icons/PrettyIcons", result["installed"])

    def test_third_party_gtk_theme(self) -> None:
        tarball = self.out / "Nordic.tar.gz"
        staging = Path(self._td.name) / "nordic"
        theme = staging / "Nordic"
        (theme / "gtk-3.0").mkdir(parents=True)
        (theme / "gtk-3.0" / "gtk.css").write_text("* { }", encoding="utf-8")
        (theme / "index.theme").write_text(
            "[Desktop Entry]\nType=X-GNOME-Metatheme\nName=Nordic\n",
            encoding="utf-8",
        )
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(theme, arcname="Nordic")
        info = self.look.inspect_archive(tarball)
        self.assertEqual(info.kind, "gtk")
        self.look.install_archive(tarball, home=self.home, apply=False)
        self.assertTrue(
            (self.home / ".local/share/themes/Nordic/gtk-3.0/gtk.css").is_file()
        )

    def test_stock_icon_theme_is_not_bundled(self) -> None:
        cfg = self.home / ".config"
        cfg.mkdir()
        (cfg / "kdeglobals").write_text("[Icons]\nTheme=breeze\n", encoding="utf-8")
        snap = self.look.inspect_look(
            self.home, {"XDG_CURRENT_DESKTOP": "KDE"}
        )
        icons = next(it for it in snap.items if it.id == "icons")
        self.assertFalse(icons.bundled)
        self.assertEqual(icons.value, "breeze")

    def test_safe_rel_rejects_tricks(self) -> None:
        self.assertIsNone(self.look._safe_rel("../etc/passwd"))
        self.assertIsNone(self.look._safe_rel("/etc/passwd"))
        self.assertIsNone(self.look._safe_rel("~/evil"))
        self.assertEqual(self.look._safe_rel("icons/Foo/index.theme"), Path("icons/Foo/index.theme"))


if __name__ == "__main__":
    unittest.main()
