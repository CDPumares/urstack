#!/usr/bin/env python3
"""Tests for catalog AppStream metadata helpers."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "lib" / "core"


def load_app_meta():
    path = CORE / "app_meta.py"
    spec = importlib.util.spec_from_file_location("app_meta", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestAppMeta(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.meta = load_app_meta()

    def test_html_to_text(self) -> None:
        raw = "<p>Fast and <b>private</b>.</p><ul><li>Tabs</li><li>Sync</li></ul>"
        text = self.meta.html_to_text(raw)
        self.assertIn("Fast and private.", text)
        self.assertIn("• Tabs", text)
        self.assertIn("• Sync", text)
        self.assertNotIn("<", text)

    def test_looks_like_flatpak_id(self) -> None:
        self.assertTrue(self.meta.looks_like_flatpak_id("org.mozilla.firefox"))
        self.assertFalse(self.meta.looks_like_flatpak_id("google-chrome-stable"))
        self.assertFalse(self.meta.looks_like_flatpak_id("https://example.com"))

    def test_pick_shot_urls(self) -> None:
        shot = {
            "sizes": [
                {"src": "https://ex/112.png", "width": "112"},
                {"src": "https://ex/624.png", "width": "624"},
                {"src": "https://ex/1504.png", "width": "1504"},
            ]
        }
        thumb, full = self.meta.pick_shot_urls(shot)
        self.assertEqual(thumb, "https://ex/624.png")
        self.assertEqual(full, "https://ex/1504.png")

    def test_normalize_appstream(self) -> None:
        appstream = {
            "name": "Firefox",
            "summary": "Fast browser",
            "description": "<p>Protects your privacy.</p>",
            "developer_name": "Mozilla",
            "project_license": "MPL-2.0",
            "categories": ["Network", "WebBrowser"],
            "urls": {"homepage": "https://www.mozilla.org/firefox/"},
            "releases": [{"version": "153.0.4"}],
            "metadata": {"flathub::verification::verified": True},
            "screenshots": [
                {
                    "sizes": [
                        {"src": "https://ex/624.png", "width": "624"},
                        {"src": "https://ex/1504.png", "width": "1504"},
                    ]
                }
            ],
        }
        out = self.meta.normalize_appstream(
            appstream, catalog_id="firefox", flathub_id="org.mozilla.firefox"
        )
        self.assertEqual(out["developer"], "Mozilla")
        self.assertEqual(out["license"], "MPL-2.0")
        self.assertEqual(out["version"], "153.0.4")
        self.assertTrue(out["verified"])
        self.assertIn("privacy", out["description"])
        self.assertEqual(out["screenshots"][0]["thumb"], "https://ex/624.png")

    def test_meta_for_row_bundled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            catalog = Path(raw)
            payload = {
                "version": 1,
                "apps": {
                    "firefox": {
                        "id": "firefox",
                        "description": "A browser.",
                        "developer": "Mozilla",
                    }
                },
            }
            (catalog / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")
            row = {"id": "firefox", "method": "flatpak", "package": "org.mozilla.firefox"}
            hit = self.meta.meta_for_row(row, catalog_dir=catalog)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["developer"], "Mozilla")
            self.assertIsNone(
                self.meta.meta_for_row({"id": "missing"}, catalog_dir=catalog)
            )

    def test_flathub_id_from_package_and_icon_map(self) -> None:
        row = {"id": "firefox", "method": "flatpak", "package": "org.mozilla.firefox"}
        self.assertEqual(self.meta.flathub_id_for_row(row), "org.mozilla.firefox")
        with tempfile.TemporaryDirectory() as raw:
            catalog = Path(raw)
            (catalog / "icon-map.json").write_text(
                json.dumps(
                    {
                        "icons": {
                            "chrome": {"icon_id": "com.google.Chrome"},
                            "edge": {
                                "icon": "https://dl.flathub.org/media/icons/128x128/com.microsoft.Edge.png"
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            mapped = self.meta.flathub_id_for_row(
                {"id": "chrome", "method": "dnf", "package": "google-chrome-stable"},
                catalog_dir=catalog,
            )
            self.assertEqual(mapped, "com.google.Chrome")
            self.assertEqual(
                self.meta.flathub_id_for_row(
                    {"id": "edge", "method": "browser", "package": "microsoft-edge-stable"},
                    catalog_dir=catalog,
                ),
                "com.microsoft.Edge",
            )

    def test_names_close(self) -> None:
        self.assertTrue(self.meta.names_close("Cursor", "Cursor (code editor)"))
        self.assertTrue(self.meta.names_close("DaVinci Resolve", "DaVinci Resolve"))
        self.assertFalse(self.meta.names_close("Ollama", "Chat for Ollama"))

    def test_catalog_fallback_meta(self) -> None:
        row = {
            "id": "cursor",
            "name": "Cursor",
            "summary": "AI code editor",
            "url": "https://cursor.com",
        }
        wiki = {"extract": "Cursor is an AI-assisted code editor.", "description": "Code editor"}
        out = self.meta.catalog_fallback_meta(row, wiki=wiki, developer="Anysphere")
        self.assertEqual(out["source"], "wikipedia")
        self.assertEqual(out["developer"], "Anysphere")
        self.assertIn("AI-assisted", out["description"])
        self.assertEqual(out["homepage"], "https://cursor.com")
        self.assertEqual(
            self.meta.flathub_id_from_icon_url(
                "https://dl.flathub.org/media/icons/128x128/com.google.Chrome.png"
            ),
            "com.google.Chrome",
        )
        self.assertEqual(
            self.meta.flathub_id_from_icon_url(
                "https://cdn.jsdelivr.net/npm/simple-icons@v13/icons/notion.svg"
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
