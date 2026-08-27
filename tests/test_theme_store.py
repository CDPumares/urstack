#!/usr/bin/env python3
"""OCS theme listings: parse, skip HTML/paid files, pick a real archive."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE_PY = ROOT / "lib" / "core" / "theme_store.py"
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


LIST_PAYLOAD = {
    "data": [
        {
            "id": "1357889",
            "name": "Orchis theme",
            "summary": "A Material Design theme",
            "personid": "vinceliuice",
            "downloads": "1234567",
            "score": "92",
            "typename": "GTK 3.x",
            "smallpreviewpic1": "https://example.test/orchis.png",
            "detailpage": "https://www.gnome-look.org/p/1357889",
        },
        {"id": "", "name": "skip me"},
        {"id": "1", "name": ""},
    ]
}

ITEM_ORCHIS = {
    "id": "1357889",
    "name": "Orchis theme",
    "downloadlink1": "https://github.com/vinceliuice/Orchis-theme",
    "downloadname1": "Orchis-theme",
    "downloadtags1": "mimetype=text/html",
    "downloadprice1": "0",
    "downloadlink2": "https://example.test/paid.zip",
    "downloadname2": "Orchis-paid.zip",
    "downloadtags2": "mimetype=application/zip",
    "downloadprice2": "4.99",
    "downloadlink3": "https://example.test/Orchis.tar.xz",
    "downloadname3": "Orchis.tar.xz",
    "downloadtags3": "mimetype=application/x-xz",
    "downloadprice3": "0",
    "downloadlink4": "https://example.test/Orchis.zip",
    "downloadname4": "Orchis.zip",
    "downloadtags4": "mimetype=application/zip",
    "downloadprice4": "0",
}


class ThemeStoreTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.store = load_store()

    def test_parse_list_skips_empty_and_keeps_fields(self) -> None:
        rows = self.store.parse_list(LIST_PAYLOAD)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["id"], "1357889")
        self.assertEqual(row["name"], "Orchis theme")
        self.assertEqual(row["author"], "vinceliuice")
        self.assertEqual(row["preview"], "https://example.test/orchis.png")

    def test_parse_list_ocs_wrapper(self) -> None:
        rows = self.store.parse_list({"ocs": {"data": LIST_PAYLOAD["data"]}})
        self.assertEqual(rows[0]["id"], "1357889")

    def test_pick_download_skips_html_and_paid(self) -> None:
        picked = self.store.pick_download(ITEM_ORCHIS)
        self.assertIsNotNone(picked)
        url, name = picked
        self.assertEqual(name, "Orchis.tar.xz")
        self.assertTrue(url.endswith("Orchis.tar.xz"))

    def test_pick_download_none_when_only_homepage(self) -> None:
        item = {
            "downloadlink1": "https://github.com/vinceliuice/Orchis-theme",
            "downloadname1": "Orchis-theme",
            "downloadtags1": "mimetype=text/html",
            "downloadprice1": "0",
        }
        self.assertIsNone(self.store.pick_download(item))

    def test_categories_follow_desktop(self) -> None:
        gnome = self.store.categories_for("gnome")
        self.assertIn("gtk", gnome)
        self.assertIn("shell", gnome)
        self.assertNotIn("plasma", gnome)
        plasma = self.store.categories_for("plasma")
        self.assertIn("plasma", plasma)
        self.assertNotIn("shell", plasma)
        xfce = self.store.categories_for("xfce")
        self.assertIn("gtk", xfce)
        self.assertNotIn("plasma", xfce)
        self.assertNotIn("shell", xfce)

    def test_default_kind(self) -> None:
        self.assertEqual(self.store.default_kind("plasma"), "plasma")
        self.assertEqual(self.store.default_kind("gnome"), "gtk")
        self.assertEqual(self.store.default_kind("xfce"), "gtk")

    def test_source_prefers_matching_store(self) -> None:
        self.assertEqual(self.store.source_for("gtk", "gnome")[0], "gnome-look")
        self.assertEqual(self.store.source_for("gtk", "plasma")[0], "kde-look")
        self.assertEqual(self.store.source_for("gtk", "xfce")[0], "gnome-look")
        self.assertEqual(self.store.source_for("plasma", "plasma"), ("kde-look", 722))

    def test_format_count(self) -> None:
        self.assertEqual(self.store.format_count("1234567"), "1.2M")
        self.assertEqual(self.store.format_count("12000"), "12k")
        self.assertEqual(self.store.format_count("12"), "12")

    def test_safe_filename_strips_paths(self) -> None:
        name = self.store.safe_filename("../evil/Orchis Theme.tar.xz")
        self.assertEqual(name, "Orchis_Theme.tar.xz")
        self.assertNotIn("..", name)
        self.assertNotIn("/", name)

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
