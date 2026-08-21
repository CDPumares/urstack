#!/usr/bin/env python3
"""Sidebar icon picking: skip full-color SVGs misnamed as symbolic."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_ui():
    import sys

    sys.path.insert(0, str(ROOT / "lib" / "core"))
    import ui  # noqa: E402

    return ui


class TestSvgReadsAsSymbolic(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ui = load_ui()

    def test_breeze_current_color_is_symbolic(self) -> None:
        svg = '<svg><style>.ColorScheme-Text { color: #fcfcfc; }</style>'
        svg += '<path style="fill:currentColor" d="M0 0"/></svg>'
        self.assertTrue(self.ui.svg_reads_as_symbolic(svg))

    def test_adwaita_single_fill_is_symbolic(self) -> None:
        svg = '<svg><path d="M0 0" fill="#2e3436"/></svg>'
        self.assertTrue(self.ui.svg_reads_as_symbolic(svg))

    def test_full_color_panel_is_not_symbolic(self) -> None:
        svg = (
            '<svg><rect fill="#2d3033"/><path fill="#3daee9"/>'
            '<circle fill="#fafafa"/><path fill="#000000"/></svg>'
        )
        self.assertFalse(self.ui.svg_reads_as_symbolic(svg))

    def test_settings_candidates_prefer_configure(self) -> None:
        self.assertEqual(self.ui.PAGE_ICON_CANDIDATES["settings"][0], "configure-symbolic")


if __name__ == "__main__":
    unittest.main()
