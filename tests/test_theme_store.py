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
            self.assertTrue(row["preview"].startswith("https://"))

    def test_all_is_the_default_and_lists_every_pick(self) -> None:
        self.assertEqual(self.store.default_kind("gnome"), "all")
        self.assertEqual(self.store.default_kind("plasma"), "all")
        gnome = self.store.categories_for("gnome")
        self.assertEqual(gnome[0], "all")
        self.assertIn("looks", gnome)
        self.assertNotIn("plasma", gnome)
        all_rows, _ = self.store.list_themes("all", "gnome", include_ocs=False)
        looks, _ = self.store.list_themes("looks", "gnome", include_ocs=False)
        self.assertGreater(len(all_rows), len(looks))
        self.assertGreaterEqual(len(all_rows), 10)

    def test_list_hides_plasma_colours_on_gnome(self) -> None:
        gnome, _label = self.store.list_themes("plasma", "gnome", include_ocs=False)
        self.assertEqual(gnome, [])
        kde, _ = self.store.list_themes("plasma", "plasma", include_ocs=False)
        self.assertTrue(any(r["id"].startswith("catppuccin-plasma") for r in kde))

    def test_search_finds_nord(self) -> None:
        rows, _ = self.store.list_themes("looks", "gnome", search="nord", include_ocs=False)
        ids = {r["id"] for r in rows}
        self.assertIn("nordic", ids)
        self.assertNotIn("dracula-gtk", ids)

    def test_parse_list_reads_ocs_previews(self) -> None:
        rows = self.store.parse_list(
            {
                "data": [
                    {
                        "id": "1357889",
                        "name": "Orchis gtk theme",
                        "summary": "Material",
                        "personid": "vinceliuice",
                        "downloads": "12",
                        "smallpreviewpic1": "https://images.pling.com/orchis.jpg",
                        "detailpage": "https://www.gnome-look.org/p/1357889",
                    }
                ]
            }
        )
        self.assertEqual(rows[0]["preview"], "https://images.pling.com/orchis.jpg")
        self.assertEqual(rows[0]["id"], "1357889")

    def test_parse_detail_keeps_screenshots_and_plain_description(self) -> None:
        parsed = self.store.parse_detail(
            {
                "id": "42",
                "name": "Orchis",
                "summary": "Material GTK",
                "description": "<p>A <b>clean</b> theme.<br>Second line.</p>",
                "personid": "vinceliuice",
                "license": "GPL-3.0",
                "version": "2024",
                "downloads": "12000",
                "smallpreviewpic1": "https://images.pling.com/orchis-sm.jpg",
                "previewpic1": "https://images.pling.com/orchis.jpg",
                "previewpic2": "https://images.pling.com/orchis-2.jpg",
                "detailpage": "https://www.gnome-look.org/p/42",
            }
        )
        self.assertEqual(parsed["description"], "A clean theme.\nSecond line.")
        self.assertEqual(len(parsed["screenshots"]), 2)
        self.assertEqual(parsed["screenshots"][0]["full"], "https://images.pling.com/orchis.jpg")
        self.assertEqual(parsed["screenshots"][0]["thumb"], "https://images.pling.com/orchis-sm.jpg")
        self.assertEqual(parsed["screenshots"][1]["full"], "https://images.pling.com/orchis-2.jpg")

    def test_details_from_row_uses_catalog_preview(self) -> None:
        nordic = self.store.catalog_entry("nordic")
        assert nordic is not None
        info = self.store.details_from_row(nordic)
        self.assertEqual(info["source"], "GitHub")
        self.assertTrue(info["screenshots"])
        self.assertEqual(info["screenshots"][0]["full"], nordic["preview"])
        self.assertIn("Nord", info["description"])
        catalog_only = self.store.fetch_details(nordic)
        self.assertEqual(catalog_only["screenshots"], info["screenshots"])

    def test_pick_download_skips_html_and_paid(self) -> None:
        picked = self.store.pick_download(
            {
                "downloadlink1": "https://github.com/example/theme",
                "downloadname1": "homepage",
                "downloadtags1": "mimetype=text/html",
                "downloadprice1": "0",
                "downloadlink2": "https://example.test/paid.zip",
                "downloadname2": "paid.zip",
                "downloadtags2": "mimetype=application/zip",
                "downloadprice2": "5",
                "downloadlink3": "https://example.test/theme.tar.xz",
                "downloadname3": "theme.tar.xz",
                "downloadtags3": "mimetype=application/x-xz",
                "downloadprice3": "0",
            }
        )
        self.assertEqual(picked[1], "theme.tar.xz")

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
            rows, label = self.store.list_themes(
                "gtk", "gnome", catalog=path, include_ocs=False
            )
            self.assertEqual(label, "GitHub picks")
            self.assertEqual(rows[0]["id"], "demo")
            self.assertEqual(rows[0]["host"], "catalog")
            self.assertIn("opengraph.githubassets.com", rows[0]["preview"])

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
        self.assertEqual(icons.PAGE_ICON_CANDIDATES["look"][0], "image-x-generic-symbolic")


if __name__ == "__main__":
    unittest.main()
