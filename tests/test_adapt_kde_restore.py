#!/usr/bin/env python3
"""Plasma restore path/activity rewriting and dnf5 install syntax."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT / "lib" / "core"))
import adapt_kde_restore as kde  # noqa: E402


OLD_ACT = "aca255f3-2bdb-4633-a7a5-942a54ae60d0"
NEW_ACT = "0bf13a5c-7649-4798-aea8-00df5e1fdad4"


class TestRewriteHome(unittest.TestCase):
    def test_file_url_and_plain_path(self) -> None:
        src = (
            "Image=file:///home/cp/Pictures/wall.png\n"
            "customButtonImage=/home/cp/Pictures/fedoraUP.png\n"
        )
        out = kde.rewrite_home_paths(src, "/home/cp", "/home/cpumares")
        self.assertIn("file:///home/cpumares/Pictures/wall.png", out)
        self.assertIn("/home/cpumares/Pictures/fedoraUP.png", out)
        self.assertNotIn("/home/cp/", out)

    def test_does_not_eat_a_longer_username(self) -> None:
        src = "Image=file:///home/cpumares/Pictures/wall.png\n"
        out = kde.rewrite_home_paths(src, "/home/cp", "/home/other")
        self.assertEqual(out, src)

    def test_detects_embedded_home(self) -> None:
        text = "Image=file:///home/cp/Pictures/a.png\n"
        self.assertEqual(kde.detect_embedded_homes(text), ["/home/cp"])


class TestActivityRemap(unittest.TestCase):
    def test_reads_current_activity(self) -> None:
        rc = "[activities]\ncurrent=aaa\n{0}=Default\n".format(NEW_ACT)
        # current takes precedence
        self.assertEqual(kde.current_activity_id(rc), "aaa")
        rc = f"[activities]\n{NEW_ACT}=Default\n"
        self.assertEqual(kde.current_activity_id(rc), NEW_ACT)

    def test_retargets_backup_containments(self) -> None:
        appletsrc = f"activityId={OLD_ACT}\nscreenMapping=desktop:/x.desktop,0,{OLD_ACT}\n"
        out = kde.remap_unmapped_activities(appletsrc, {NEW_ACT}, NEW_ACT)
        self.assertIn(NEW_ACT, out)
        self.assertNotIn(OLD_ACT, out)

    def test_leaves_known_ids_alone(self) -> None:
        text = f"activityId={NEW_ACT}\n"
        out = kde.remap_unmapped_activities(text, {NEW_ACT}, NEW_ACT)
        self.assertEqual(out, text)


class TestColorSchemeExport(unittest.TestCase):
    def test_fills_panel_colours_from_window(self) -> None:
        src = (
            "[Colors:Window]\n"
            "BackgroundAlternate=19,25,28\n"
            "BackgroundNormal=14,18,24\n"
            "ForegroundNormal=179,177,173\n"
            "\n"
            "[Colors:Complementary]\n"
            "DecorationFocus=61,174,233\n"
            "\n"
            "[General]\n"
            "AccentColor=61,174,233\n"
        )
        out = kde.complete_colorscheme(src)
        self.assertIn("ColorScheme=UrStackRestored", out)
        self.assertIn("[Colors:Complementary]", out)
        self.assertIn("BackgroundNormal=14,18,24", out)
        self.assertIn("[Colors:Header]", out)
        self.assertIn("AccentColor=61,174,233", out)
        self.assertNotIn(" = ", out)
        # Panel chrome must not inherit Window's leftover light Alternate.
        header = out.split("[Colors:Header]", 1)[1]
        self.assertIn("BackgroundAlternate=14,18,24", header)
        self.assertNotIn("BackgroundAlternate=19,25,28", header)

    def test_keeps_explicit_complementary_background(self) -> None:
        src = (
            "[Colors:Window]\n"
            "BackgroundNormal=14,18,24\n"
            "\n"
            "[Colors:Complementary]\n"
            "BackgroundNormal=1,2,3\n"
        )
        out = kde.complete_colorscheme(src)
        self.assertIn("BackgroundNormal=1,2,3", out)
        self.assertIn("BackgroundNormal=14,18,24", out)


class TestAdaptHome(unittest.TestCase):
    def test_new_machine_gets_live_activity_and_home(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            cfg = home / ".config"
            cfg.mkdir()
            (cfg / "plasma-org.kde.plasma.desktop-appletsrc").write_text(
                f"activityId={OLD_ACT}\n"
                "Image=file:///home/cp/Pictures/wall.png\n",
                encoding="utf-8",
            )
            (cfg / "kactivitymanagerdrc").write_text(
                f"[activities]\n{NEW_ACT}=Default\n",
                encoding="utf-8",
            )
            report = kde.adapt_restored_home(home, keep_activity=NEW_ACT)
            text = (cfg / "plasma-org.kde.plasma.desktop-appletsrc").read_text(
                encoding="utf-8"
            )
            self.assertEqual(report["activity"], NEW_ACT)
            self.assertIn(NEW_ACT, text)
            self.assertNotIn(OLD_ACT, text)
            self.assertIn(f"file://{home}/Pictures/wall.png", text)

    def test_rewrites_dolphin_places_without_touching_disk_uuids(self) -> None:
        disk = "4cbae237-b440-42b6-a149-c36567996fda"
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            share = home / ".local/share"
            share.mkdir(parents=True)
            (share / "user-places.xbel").write_text(
                '<bookmark href="file:///home/cp/Documents"/>\n'
                f"<uuid>{disk}</uuid>\n",
                encoding="utf-8",
            )
            kde.adapt_restored_home(home, keep_activity=NEW_ACT)
            text = (share / "user-places.xbel").read_text(encoding="utf-8")
            self.assertIn(f"file://{home}/Documents", text)
            self.assertIn(disk, text)
            self.assertNotIn("/home/cp/", text)


    def test_backup_strips_username_from_overlay_and_extra(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ov = root / "config" / "home-overlay" / ".config"
            ov.mkdir(parents=True)
            (ov / "appletsrc").write_text(
                "Image=file:///home/cp/Pictures/wall.png\n", encoding="utf-8"
            )
            extra = root / "extra" / "Documents"
            extra.mkdir(parents=True)
            (extra / "notes.txt").write_text(
                "open /home/cp/Documents/file.txt\n", encoding="utf-8"
            )
            (extra / "photo.png").write_bytes(b"\x89PNG\r\n")
            git = extra / ".git"
            git.mkdir()
            (git / "config").write_text("path=/home/cp/Documents\n", encoding="utf-8")
            n = kde.portable_backup_tree(root, "/home/cp")
            self.assertGreaterEqual(n, 2)
            appletsrc = (ov / "appletsrc").read_text(encoding="utf-8")
            notes = (extra / "notes.txt").read_text(encoding="utf-8")
            self.assertIn(kde.HOME_TOKEN, appletsrc)
            self.assertIn(kde.HOME_TOKEN, notes)
            self.assertNotIn("/home/cp", appletsrc)
            self.assertNotIn("/home/cp", notes)
            self.assertEqual((extra / "photo.png").read_bytes(), b"\x89PNG\r\n")
            self.assertIn("/home/cp", (git / "config").read_text(encoding="utf-8"))
            self.assertTrue((root / "manifests" / "portable-home.txt").is_file())

    def test_restore_expands_token_onto_this_user(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            backup = Path(d) / "bak"
            live = Path(d) / "live"
            ov = backup / "config" / "home-overlay" / ".local" / "share"
            ov.mkdir(parents=True)
            (ov / "user-places.xbel").write_text(
                f'<bookmark href="file://{kde.HOME_TOKEN}/Pictures"/>\n',
                encoding="utf-8",
            )
            dest = live / ".local" / "share"
            dest.mkdir(parents=True)
            (dest / "user-places.xbel").write_bytes(
                (ov / "user-places.xbel").read_bytes()
            )
            n = kde.materialize_backup_home(backup, live)
            self.assertEqual(n, 1)
            text = (dest / "user-places.xbel").read_text(encoding="utf-8")
            self.assertIn(f"file://{live}/Pictures", text)
            self.assertNotIn(kde.HOME_TOKEN, text)
            self.assertNotIn("/home/cp", text)
    def test_copies_wallpaper_into_portable_dir(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            overlay = Path(d) / "overlay"
            pic = home / "Pictures"
            pic.mkdir(parents=True)
            wall = pic / "wall.png"
            wall.write_bytes(b"PNG")
            cfg = overlay / ".config"
            cfg.mkdir(parents=True)
            (cfg / "plasma-org.kde.plasma.desktop-appletsrc").write_text(
                f"Image=file://{wall}\n",
                encoding="utf-8",
            )
            n = kde.collect_plasma_media(home, overlay)
            self.assertEqual(n, 1)
            stored = overlay / kde.PORTABLE_WALLPAPER_REL / "wall.png"
            self.assertTrue(stored.is_file())
            text = (cfg / "plasma-org.kde.plasma.desktop-appletsrc").read_text(
                encoding="utf-8"
            )
            self.assertIn(str(home / kde.PORTABLE_WALLPAPER_REL / "wall.png"), text)

    def test_finds_slideshow_paths(self) -> None:
        text = "SlidePaths=/home/cp/Pictures/a.png,/home/cp/Pictures/b.png\n"
        paths = kde.media_paths_from_config(text)
        self.assertEqual(
            paths,
            ["/home/cp/Pictures/a.png", "/home/cp/Pictures/b.png"],
        )


class TestEphemeralLaunchers(unittest.TestCase):
    def test_strips_pwas_and_android_apps_keeps_chrome(self) -> None:
        src = (
            "launchers=applications:org.kde.dolphin.desktop,"
            "applications:google-chrome.desktop,"
            "applications:chrome-aojolhcflnnbllbpmfekidpadkeoobcm-Default.desktop,"
            "applications:Waydroid.desktop,"
            "applications:waydroid.com.android.chrome.desktop,"
            "applications:cursor.desktop\n"
        )
        out = kde.filter_ephemeral_launchers(src, drop_waydroid_pin=True)
        self.assertIn("google-chrome.desktop", out)
        self.assertIn("org.kde.dolphin.desktop", out)
        self.assertIn("cursor.desktop", out)
        self.assertNotIn("chrome-aoj", out)
        self.assertNotIn("Waydroid.desktop", out)
        self.assertNotIn("waydroid.com", out)

    def test_keeps_waydroid_pin_when_requested(self) -> None:
        src = "launchers=applications:Waydroid.desktop,applications:urstack.desktop\n"
        out = kde.filter_ephemeral_launchers(src, drop_waydroid_pin=False)
        self.assertIn("Waydroid.desktop", out)
        self.assertIn("urstack.desktop", out)

    def test_prune_deletes_pwa_and_waydroid_desktops(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            apps = home / ".local" / "share" / "applications"
            apps.mkdir(parents=True)
            (apps / "chrome-aojolhcflnnbllbpmfekidpadkeoobcm-Default.desktop").write_text(
                "Exec=/opt/google/chrome/google-chrome --app-id=aojolhcflnnbllbpmfekidpadkeoobcm\n",
                encoding="utf-8",
            )
            (apps / "waydroid.com.android.chrome.desktop").write_text(
                "Exec=waydroid app launch com.android.chrome\nCategories=X-WayDroid-App;\n",
                encoding="utf-8",
            )
            (apps / "cursor.desktop").write_text("Exec=cursor\n", encoding="utf-8")
            cfg = home / ".config"
            cfg.mkdir()
            applets = cfg / "plasma-org.kde.plasma.desktop-appletsrc"
            applets.write_text(
                "launchers=applications:cursor.desktop,"
                "applications:chrome-aojolhcflnnbllbpmfekidpadkeoobcm-Default.desktop\n",
                encoding="utf-8",
            )
            report = kde.prune_ephemeral_launchers(home, drop_waydroid_pin=True)
            self.assertEqual(report["desktops"], "2")
            self.assertTrue((apps / "cursor.desktop").is_file())
            self.assertFalse(
                (apps / "chrome-aojolhcflnnbllbpmfekidpadkeoobcm-Default.desktop").exists()
            )
            text = applets.read_text(encoding="utf-8")
            self.assertIn("cursor.desktop", text)
            self.assertNotIn("chrome-aoj", text)


class TestDesktopDetect(unittest.TestCase):
    def test_kde_and_gnome(self) -> None:
        self.assertEqual(
            kde.detect_desktop_environment({"XDG_CURRENT_DESKTOP": "KDE"}),
            "kde",
        )
        self.assertEqual(
            kde.detect_desktop_environment({"XDG_CURRENT_DESKTOP": "GNOME"}),
            "gnome",
        )
        self.assertEqual(
            kde.detect_desktop_environment({"XDG_CURRENT_DESKTOP": "XFCE"}),
            "xfce",
        )
        self.assertEqual(
            kde.detect_desktop_environment({"DESKTOP_SESSION": "hyprland"}),
            "hyprland",
        )
        self.assertEqual(kde.detect_desktop_environment({}), "all")


class TestBackupCapturesDesktopBits(unittest.TestCase):
    def test_overlay_lists_include_missed_theme_files(self) -> None:
        text = (ROOT / "lib/plugins/backup.sh").read_text(encoding="utf-8")
        for needle in (
            "kdedefaults",
            "kactivitymanagerd-statsrc",
            "ksplashrc",
            "breezerc",
            "krunnerrc",
            "export-colorscheme",
            ".local/share/gnome-shell",
            ".config/dconf",
            "_desktop_wants",
            "_backup_opt desktop",
            "prune-launchers",
        ):
            self.assertIn(needle, text, f"backup.sh missing {needle}")

    def test_ui_has_desktop_presets(self) -> None:
        text = (ROOT / "lib/core/ui.py").read_text(encoding="utf-8")
        self.assertIn("DESKTOP_PRESETS", text)
        self.assertIn('("kde", "KDE")', text)
        self.assertIn('("gnome", "GNOME")', text)

    def test_ui_keeps_include_presets(self) -> None:
        text = (ROOT / "lib/core/ui.py").read_text(encoding="utf-8")
        self.assertIn("BACKUP_PRESETS", text)
        self.assertIn("RESTORE_PRESETS", text)
        self.assertIn("This computer", text)
        self.assertIn("New computer", text)
        self.assertIn("No secrets", text)
        self.assertIn("Packages only", text)

    def test_restore_shows_failures_and_offers_reboot(self) -> None:
        text = (ROOT / "lib/plugins/backup.sh").read_text(encoding="utf-8")
        self.assertIn("_restore_finish_ui", text)
        self.assertIn("_restore_failed_bullets", text)
        self.assertIn("Reboot recommended", text)
        self.assertIn("# Failed:", text)


class TestDnf5InstallSyntax(unittest.TestCase):
    def test_install_does_not_pass_bare_end_of_options(self) -> None:
        """dnf5 errors on `dnf install -y -- pkgs`; names are validated instead."""
        needle = r"dnf install -y --(?:\s|$)"
        import re

        for rel in (
            "lib/plugins/backup.sh",
            "lib/core/catalog.sh",
        ):
            text = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIsNone(
                re.search(needle, text),
                f"{rel} still uses dnf install -y -- which dnf5 rejects",
            )


if __name__ == "__main__":
    unittest.main()
