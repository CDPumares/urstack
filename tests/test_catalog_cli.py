#!/usr/bin/env python3
"""CLIs and language toolchains live in their own catalog category."""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data" / "catalog"
IMPORTER = ROOT / "scripts" / "import-winutil-apps.py"

GUI_DEVELOPER_IDS = {
    "cursor",
    "vscode",
    "vscodium",
    "zed",
    "android-studio",
    "github-desktop",
    "postman",
}

CLI_IDS = {
    "git",
    "gh",
    "neovim",
    "python3",
    "golang",
    "rust",
    "nodejs",
    "ollama",
    "ngrok",
    "podman",
    "uv",
}


def _by_category(name: str) -> dict[str, set[str]]:
    data = json.loads((CATALOG / name).read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for cat in data.get("categories", []):
        cid = cat.get("id") or ""
        out[cid] = {app.get("id") for app in cat.get("apps", [])}
    return out


def _importer_cli_keys() -> set[str]:
    tree = ast.parse(IMPORTER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "CLI_KEYS":
                    return set(ast.literal_eval(node.value))
    raise AssertionError("CLI_KEYS not found in import-winutil-apps.py")


class TestCatalogCliCategory(unittest.TestCase):
    def test_apps_json_has_cli_category(self) -> None:
        cats = _by_category("apps.json")
        self.assertIn("cli", cats)
        self.assertTrue(cats["cli"])
        for aid in CLI_IDS:
            self.assertIn(aid, cats["cli"], msg=f"{aid} should be under CLIs & tools")
            self.assertNotIn(aid, cats.get("developer", set()))

    def test_developer_keeps_gui_apps(self) -> None:
        cats = _by_category("apps.json")
        for aid in GUI_DEVELOPER_IDS:
            self.assertIn(aid, cats.get("developer", set()), msg=aid)

    def test_winutil_clis_are_not_in_developer(self) -> None:
        cats = _by_category("winutil.json")
        self.assertIn("cli", cats)
        winutil_ids = cats["cli"]
        self.assertTrue(any(i.endswith("githubcli") or i.endswith("git") for i in winutil_ids))
        for aid in cats.get("developer", set()):
            self.assertFalse(
                aid.endswith("-git") or aid.endswith("-python3") or aid.endswith("-neovim"),
                msg=f"{aid} is a CLI still listed as a developer app",
            )

    def test_importer_routes_cli_keys(self) -> None:
        keys = _importer_cli_keys()
        self.assertIn("git", keys)
        self.assertIn("githubcli", keys)
        self.assertIn("rustlang", keys)
        self.assertNotIn("vscode", keys)
        self.assertNotIn("cursor", keys)
