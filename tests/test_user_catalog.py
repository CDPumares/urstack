#!/usr/bin/env python3
"""Personal Apps overlay: Flathub / DNF / Snap only, no extra remotes."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib" / "core"))

import user_catalog  # noqa: E402


class TestUserCatalogValidation(unittest.TestCase):
    def test_flatpak_id_requires_reverse_dns(self) -> None:
        self.assertTrue(user_catalog.valid_package("flatpak", "org.mozilla.firefox"))
        self.assertTrue(user_catalog.valid_package("flatpak", "com.github.tchx84.Flatseal"))
        self.assertFalse(user_catalog.valid_package("flatpak", "firefox"))
        self.assertFalse(user_catalog.valid_package("flatpak", "org.mozilla"))
        self.assertFalse(user_catalog.valid_package("flatpak", "--user"))
        self.assertFalse(user_catalog.valid_package("flatpak", "org.foo.bar;id"))

    def test_dnf_and_snap_match_catalog_pkg_rule(self) -> None:
        self.assertTrue(user_catalog.valid_package("dnf", "kernel-devel"))
        self.assertTrue(user_catalog.valid_package("snap", "vlc"))
        for bad in ("--nogpgcheck", "-y", "; id", "/tmp/e.rpm", "a b", ""):
            self.assertFalse(user_catalog.valid_package("dnf", bad), bad)
            self.assertFalse(user_catalog.valid_package("snap", bad), bad)

    def test_vendor_methods_are_refused(self) -> None:
        for method in ("script", "rpm_url", "appimage", "browser", "cursor_rpm"):
            self.assertFalse(user_catalog.valid_package(method, "anything"))


class TestUserCatalogStore(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "catalog-user.json"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def test_add_and_remove_roundtrip(self) -> None:
        app = user_catalog.add_app(
            "flatpak", "org.mozilla.firefox", "Firefox", path=self.path
        )
        self.assertTrue(app["id"].startswith("user-"))
        self.assertEqual(app["method"], "flatpak")
        loaded = user_catalog.load_apps(self.path)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["package"], "org.mozilla.firefox")
        self.assertTrue(user_catalog.remove_app(app["id"], path=self.path))
        self.assertEqual(user_catalog.load_apps(self.path), [])

    def test_add_refuses_duplicate_and_curated_package(self) -> None:
        user_catalog.add_app("dnf", "vlc", path=self.path)
        with self.assertRaises(ValueError):
            user_catalog.add_app("dnf", "vlc", path=self.path)
        with self.assertRaises(ValueError):
            user_catalog.add_app(
                "flatpak",
                "org.mozilla.firefox",
                path=self.path,
                existing_packages={"org.mozilla.firefox"},
            )

    def test_add_refuses_script_method(self) -> None:
        with self.assertRaises(ValueError):
            user_catalog.add_app("script", "https://evil.example/x.sh", path=self.path)

    def test_load_skips_invalid_overlay_entries(self) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "apps": [
                        {
                            "id": "user-dnf-git",
                            "name": "Git",
                            "method": "dnf",
                            "package": "git",
                        },
                        {
                            "id": "user-script-evil",
                            "method": "script",
                            "package": "https://evil.example/x.sh",
                        },
                        {
                            "id": "user-dnf-flag",
                            "method": "dnf",
                            "package": "--nogpgcheck",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        apps = user_catalog.load_apps(self.path)
        self.assertEqual([a["package"] for a in apps], ["git"])

    def test_remove_refuses_non_user_ids(self) -> None:
        user_catalog.add_app("snap", "vlc", path=self.path)
        self.assertFalse(user_catalog.remove_app("firefox", path=self.path))
        self.assertEqual(len(user_catalog.load_apps(self.path)), 1)

    def test_as_catalog_row_marks_user_and_mine(self) -> None:
        app = user_catalog.add_app("flatpak", "org.kde.okular", path=self.path)
        row = user_catalog.as_catalog_row(app)
        self.assertEqual(row["user"], "1")
        self.assertEqual(row["category_id"], "added")
        self.assertEqual(row["method"], "flatpak")


if __name__ == "__main__":
    unittest.main()
