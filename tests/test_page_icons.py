#!/usr/bin/env python3
"""Sidebar icon picking: skip full-color SVGs misnamed as symbolic."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGE_ICONS = ROOT / "lib" / "core" / "page_icons.py"


def load_page_icons():
    spec = importlib.util.spec_from_file_location("page_icons", PAGE_ICONS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSvgReadsAsSymbolic(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.icons = load_page_icons()

    def test_breeze_current_color_is_symbolic(self) -> None:
        svg = '<svg><style>.ColorScheme-Text { color: #fcfcfc; }</style>'
        svg += '<path style="fill:currentColor" d="M0 0"/></svg>'
        self.assertTrue(self.icons.svg_reads_as_symbolic(svg))

    def test_adwaita_single_fill_is_symbolic(self) -> None:
        svg = '<svg><path d="M0 0" fill="#2e3436"/></svg>'
        self.assertTrue(self.icons.svg_reads_as_symbolic(svg))

    def test_full_color_panel_is_not_symbolic(self) -> None:
        svg = (
            '<svg><rect fill="#2d3033"/><path fill="#3daee9"/>'
            '<circle fill="#fafafa"/><path fill="#000000"/></svg>'
        )
        self.assertFalse(self.icons.svg_reads_as_symbolic(svg))

    def test_settings_candidates_prefer_configure(self) -> None:
        self.assertEqual(
            self.icons.PAGE_ICON_CANDIDATES["settings"][0],
            "configure-symbolic",
        )

    def test_look_candidates_include_theme_symbolic(self) -> None:
        self.assertEqual(
            self.icons.PAGE_ICON_CANDIDATES["look"][0],
            "preferences-desktop-theme-symbolic",
        )


if __name__ == "__main__":
    unittest.main()
