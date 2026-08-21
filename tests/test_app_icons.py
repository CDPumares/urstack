#!/usr/bin/env python3
"""Tests for catalog icon path helpers."""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "lib" / "core"


def load_app_icons():
    path = CORE / "app_icons.py"
    spec = importlib.util.spec_from_file_location("app_icons", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAppIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.icons = load_app_icons()

    def test_safe_icon_stem(self) -> None:
        self.assertEqual(self.icons.safe_icon_stem("firefox"), "firefox")
        self.assertEqual(self.icons.safe_icon_stem("winutil-onedrive"), "winutil-onedrive")
        self.assertEqual(self.icons.safe_icon_stem("1password"), "1password")
        self.assertEqual(self.icons.safe_icon_stem("foo/bar"), "foo_bar")

    def test_bundled_icon_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            catalog = Path(raw)
            icons = catalog / "icons"
            icons.mkdir()
            png = icons / "firefox.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            hit = self.icons.bundled_icon_path("firefox", catalog_dir=catalog)
            self.assertEqual(hit, png)
            self.assertIsNone(self.icons.bundled_icon_path("missing", catalog_dir=catalog))
            self.assertIsNone(self.icons.bundled_icon_path("", catalog_dir=catalog))

    def test_icon_path_for_row_prefers_bundled(self) -> None:
        bundled = self.icons.bundled_icons_dir()
        if not bundled.is_dir():
            self.skipTest("bundled icons not vendored yet")
        sample = next(bundled.glob("*.png"), None)
        if sample is None:
            self.skipTest("no bundled pngs")
        row = {"id": sample.stem, "method": "", "package": "", "icon": ""}
        path = self.icons.icon_path_for_row(row)
        self.assertEqual(path, sample)


if __name__ == "__main__":
    unittest.main()
