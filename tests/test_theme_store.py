#!/usr/bin/env python3
"""Curated GitHub theme catalog: filter, resolve URLs, skip Windows assets."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PY = ROOT / "lib" / "core" / "theme_store.py"
CATALOG = ROOT / "data" / "catalog" / "themes.json"
LOOK_ICON = ROOT / "data" / "icons" / "hicolor" / "scalable" / "apps" / "urstack-look-symbolic.svg"


def load_store():
    import sys

    existing = sys.modules.get("urstack_theme_store")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location("urstack_theme_store", STORE_PY)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["urstack_theme_store"] = mod
    spec.loader.exec_module(mod)
    return mod


class ThemeStoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = load_store()
        cls.themes = cls.store.load_catalog()

    def test_shipped_catalog_has_community_palettes(self) -> None:
        ids = {row["id"] for row in self.themes}
        for want in (
            "dracula-gtk",
            "nordic",
            "sweet",
            "catppuccin-mocha",
            "candy-icons",
            "bibata-modern-classic",
        ):
            self.assertIn(want, ids)
        self.assertGreaterEqual(len(self.themes), 10)
        for row in self.themes:
            self.assertTrue(row["github"].count("/") == 1)
            self.assertTrue(row["asset"] or row["ref"])
            self.assertTrue(self.store.entry_kinds(row))

    def test_catalog_json_is_valid(self) -> None:
        payload = json.loads(CATALOG.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("source"), "github")
        self.assertIsInstance(payload.get("themes"), list)

    def test_looks_is_the_default_and_plasma_is_plasma_only(self) -> None:
        self.assertEqual(self.store.default_kind("gnome"), "looks")
        self.assertEqual(self.store.default_kind("plasma"), "looks")
        gnome = self.store.categories_for("gnome")
        self.assertIn("looks", gnome)
        self.assertIn("gtk", gnome)
        self.assertNotIn("plasma", gnome)
        plasma = self.store.categories_for("plasma")
        self.assertIn("plasma", plasma)

    def test_list_hides_plasma_colours_on_gnome(self) -> None:
        gnome, _label = self.store.list_themes("plasma", "gnome")
        self.assertEqual(gnome, [])
        kde, _ = self.store.list_themes("plasma", "plasma")
        self.assertTrue(any(r["id"].startswith("catppuccin-plasma") for r in kde))

    def test_search_finds_nord(self) -> None:
        rows, _ = self.store.list_themes("looks", "gnome", search="nord")
        ids = {r["id"] for r in rows}
        self.assertIn("nordic", ids)
        self.assertNotIn("dracula-gtk", ids)

    def test_resolve_release_and_source_urls(self) -> None:
        dracula = self.store.catalog_entry("dracula-gtk")
        assert dracula is not None
        url, name = self.store.resolve_download(dracula)
        self.assertIn("/releases/latest/download/", url)
        self.assertIn("Dracula.tar.xz", url)
        self.assertEqual(name, "Dracula.tar.xz")
        candy = self.store.catalog_entry("candy-icons")
        assert candy is not None
        url, name = self.store.resolve_download(candy)
        self.assertIn("/archive/refs/heads/master.tar.gz", url)
        self.assertTrue(name.endswith(".tar.gz"))

    def test_github_url_rejects_odd_repos(self) -> None:
        with self.assertRaises(self.store.ThemeStoreError):
            self.store.github_release_asset_url("../evil", "x.tar.xz")
        with self.assertRaises(self.store.ThemeStoreError):
            self.store.github_archive_url("dracula/gtk", "refs/heads/master")

    def test_pick_github_asset_skips_windows(self) -> None:
        assets = [
            {
                "name": "Bibata-Modern-Classic-Windows.zip",
                "browser_download_url": "https://example.test/win.zip",
            },
            {
                "name": "Bibata-Modern-Classic.tar.xz",
                "browser_download_url": "https://example.test/classic.tar.xz",
            },
        ]
        picked = self.store.pick_github_asset(assets, "Bibata-Modern-Classic.tar.xz")
        self.assertEqual(picked[1], "Bibata-Modern-Classic.tar.xz")

    def test_plus_in_asset_is_quoted(self) -> None:
        url = self.store.github_release_asset_url(
            "catppuccin/gtk", "catppuccin-mocha-mauve-standard+default.zip"
        )
        self.assertIn("%2B", url)

    def test_temp_catalog_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "themes.json"
            path.write_text(
                json.dumps(
                    {
                        "themes": [
                            {
                                "id": "demo",
                                "name": "Demo",
                                "github": "example/demo",
                                "kinds": ["gtk"],
                                "asset": "Demo.tar.xz",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            rows, label = self.store.list_themes("gtk", "gnome", catalog=path)
            self.assertEqual(label, "the UrStack catalog")
            self.assertEqual(rows[0]["id"], "demo")
            self.assertEqual(rows[0]["host"], "catalog")

    def test_look_icon_is_symbolic(self) -> None:
        self.assertTrue(LOOK_ICON.is_file())
        svg = LOOK_ICON.read_text(encoding="utf-8")
        self.assertIn("currentColor", svg)
        spec = importlib.util.spec_from_file_location(
            "page_icons_store_test", ROOT / "lib" / "core" / "page_icons.py"
        )
        assert spec and spec.loader
        icons = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(icons)
        self.assertTrue(icons.svg_reads_as_symbolic(svg))
        self.assertEqual(icons.PAGE_ICON_CANDIDATES["look"][0], "urstack-look-symbolic")


if __name__ == "__main__":
    unittest.main()
