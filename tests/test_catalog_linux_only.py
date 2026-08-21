#!/usr/bin/env python3
"""The Apps catalog must not list Windows-only software."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog"
IMPORTER = ROOT / "scripts" / "import-winutil-apps.py"

# Official desktop clients that are Windows/macOS only (or retired).
WINDOWS_CATALOG_IDS = {
    "skype",
    "whatsapp-desktop",
    "apple-music",
    "affinity",
    "fusion360",
    "battlenet",
    "ea-app",
    "winutil-itunes",
    "winutil-wingetui",
    "winutil-chatgpt",
    "winutil-claude",
    "winutil-nuget",
    "winutil-onedrive",
    "winutil-googledrive",
    "winutil-whatsapp",
    "winutil-terminal",
    "winutil-powertoys",
    "winutil-rufus",
}

WINDOWS_URL_NEEDLES = (
    "chromium-win64",
    "rubyinstaller.org",
    "luaforwindows",
    "unigetui",
    "marticliment.com/unigetui",
    "aka.ms/terminal",
    "github.com/microsoft/PowerToys",
    "getsharex.com",
    "www.getpaint.net",
    "irfanview.com",
    "sumatrapdfreader.org",
    "winscp.net",
    "autohotkey.com",
    "voidtools.com",
    "justgetflux.com",
    "www.apple.com/itunes",
    "www.ea.com/ea-app",
    "download.battle.net",
    "affinity.serif.com",
)


def _catalog_apps() -> list[dict]:
    rows: list[dict] = []
    for name in ("apps.json", "winutil.json"):
        data = json.loads((CATALOG / name).read_text(encoding="utf-8"))
        for cat in data.get("categories", []):
            for app in cat.get("apps", []):
                row = dict(app)
                row["_file"] = name
                rows.append(row)
    return rows


def _windows_only_from_importer() -> set[str]:
    tree = ast.parse(IMPORTER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "WINDOWS_ONLY":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("WINDOWS_ONLY not found in import-winutil-apps.py")


class TestCatalogLinuxOnly(unittest.TestCase):
    def test_no_windows_store_badge(self) -> None:
        for app in _catalog_apps():
            self.assertNotEqual(
                app.get("store"),
                "windows",
                msg=f"{app.get('id')} still marked as a Windows listing",
            )

    def test_known_windows_ids_are_gone(self) -> None:
        present = {app.get("id") for app in _catalog_apps()}
        leftover = sorted(WINDOWS_CATALOG_IDS & present)
        self.assertEqual(leftover, [])

    def test_no_windows_download_urls(self) -> None:
        hits = []
        for app in _catalog_apps():
            url = (app.get("url") or "").lower()
            for needle in WINDOWS_URL_NEEDLES:
                if needle.lower() in url:
                    hits.append(f"{app.get('id')}: {app.get('url')}")
        self.assertEqual(hits, [])

    def test_importer_denylist_covers_leaks(self) -> None:
        denylist = _windows_only_from_importer()
        for key in (
            "itunes",
            "wingetui",
            "chatgpt",
            "claude",
            "nuget",
            "onedrive",
            "googledrive",
            "eaapp",
            "powertoys",
            "terminal",
            "rufus",
        ):
            self.assertIn(key, denylist)


if __name__ == "__main__":
    unittest.main()
