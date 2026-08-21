#!/usr/bin/env python3
"""Unit tests for Fedora Workstation Updater parsers."""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "lib" / "core"


def load_ui_module():
    path = CORE / "ui.py"
    spec = importlib.util.spec_from_file_location("fedora_ui", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fedora_ui"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestParseSections(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.ui = load_ui_module()
        except Exception as exc:
            raise unittest.SkipTest(f"GTK/libadwaita unavailable: {exc}") from exc

    def test_empty(self) -> None:
        secs = self.ui.parse_sections("")
        self.assertEqual(len(secs), 1)
        self.assertEqual(secs[0].kind, "empty")

    def test_nothing_to_update(self) -> None:
        secs = self.ui.parse_sections("Nothing to update.\n\nTip: cargo needs cargo-update")
        self.assertEqual(secs[0].kind, "empty")
        self.assertIn("Nothing to update", secs[0].body)

    def test_named_sections(self) -> None:
        text = (
            "=== DNF (3 package(s)) ===\n"
            "kernel.x86_64  1  updates\n\n"
            "=== Flatpak OCI notes ===\n"
            "(oci noise) org.fedoraproject.Platform\n"
        )
        secs = self.ui.parse_sections(text)
        titles = [s.title for s in secs]
        self.assertTrue(any("DNF" in t for t in titles))
        self.assertTrue(any("OCI" in t or "Flatpak" in t for t in titles))
        oci = next(s for s in secs if "OCI" in s.title or "Flatpak" in s.title)
        self.assertEqual(oci.kind, "advisory")

    def test_parse_items_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
            fh.write("TRUE|dnf|DNF packages\n")
            fh.write("FALSE|fw|Firmware\n")
            fh.write("# comment\n")
            path = fh.name
        items = self.ui.parse_items_file(path)
        Path(path).unlink(missing_ok=True)
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].checked)
        self.assertEqual(items[0].item_id, "dnf")
        self.assertFalse(items[1].checked)


class TestFlatpakOciNoise(unittest.TestCase):
    def _noise(self, app: str) -> int:
        script = f"""
source "{CORE}/common.sh"
source "{CORE}/checks.sh"
load_updater_config
if is_flatpak_oci_noise "{app}"; then exit 0; else exit 1; fi
"""
        return subprocess.run(["bash", "-c", script], check=False).returncode

    def test_platform_is_noise(self) -> None:
        self.assertEqual(self._noise("org.fedoraproject.Platform"), 0)
        self.assertEqual(self._noise("org.fedoraproject.Gtk3theme.Adwaita"), 0)
        self.assertEqual(self._noise("org.gnome.Loupe.Locale"), 0)

    def test_real_app_not_noise(self) -> None:
        self.assertEqual(self._noise("org.mozilla.firefox"), 1)
        self.assertEqual(self._noise("com.spotify.Client"), 1)


class TestSectionSelected(unittest.TestCase):
    def _selected(self, key: str, selected: str) -> int:
        script = f"""
source "{CORE}/common.sh"
if section_is_selected "{key}" "{selected}"; then exit 0; else exit 1; fi
"""
        return subprocess.run(["bash", "-c", script], check=False).returncode

    def test_all(self) -> None:
        self.assertEqual(self._selected("pip", "all"), 0)
        self.assertEqual(self._selected("npm_user", "all"), 0)

    def test_exact_keys(self) -> None:
        self.assertEqual(self._selected("pipx", "pipx"), 0)
        self.assertEqual(self._selected("pip", "pipx"), 1)
        self.assertEqual(self._selected("npm", "npm_user"), 1)
        self.assertEqual(self._selected("npm_user", "npm_user"), 0)
        self.assertEqual(self._selected("npm", "dnf|npm|pip"), 0)
        self.assertEqual(self._selected("npm_user", "dnf|npm|pip"), 1)

    def test_labels(self) -> None:
        self.assertEqual(self._selected("pip", "pip packages"), 0)
        self.assertEqual(self._selected("pipx", "pip packages"), 1)


class TestConfigSources(unittest.TestCase):
    def test_default_core_only(self) -> None:
        script = f"""
export FEDORA_UPDATES_USER_CONFIG=/dev/null
source "{CORE}/common.sh"
FEDORA_UPDATES_ROOT="{ROOT}"
_load_conf_file "{ROOT}/config/default.conf"
load_updater_config
printf '%s\\n' "${{SECTION_KEYS[@]}}"
"""
        out = subprocess.check_output(["bash", "-c", script], text=True)
        keys = set(out.split())
        self.assertIn("dnf", keys)
        self.assertIn("flatpak", keys)
        self.assertNotIn("cursor", keys)
        self.assertNotIn("npm", keys)


class TestCatalogRows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.ui = load_ui_module()
        except Exception as exc:
            raise unittest.SkipTest(f"GTK/libadwaita unavailable: {exc}") from exc

    def test_load_catalog_rows_optional_fields(self) -> None:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
            fh.write(
                "firefox|Firefox|Private browser|Browsers|browsers|"
                "flatpak|org.mozilla.firefox|0||flatpak|https://example/icon.png|\n"
            )
            fh.write(
                "chrome|Google Chrome|Vendor browser|Browsers|browsers|"
                "dnf|google-chrome-stable|1|https://www.google.com/chrome/|vendor||"
                "Needs the google-chrome yum repo\n"
            )
            path = fh.name
        rows = self.ui._load_catalog_rows(Path(path))
        Path(path).unlink(missing_ok=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["id"], "firefox")
        self.assertEqual(rows[0]["repo_hint"], "")
        self.assertEqual(rows[1]["url"], "https://www.google.com/chrome/")
        self.assertEqual(rows[1]["repo_hint"], "Needs the google-chrome yum repo")
        self.assertEqual(
            self.ui._catalog_install_choice(rows[0]),
            "install|flatpak|org.mozilla.firefox|Firefox|",
        )
        self.assertIn("Flathub", self.ui._app_source_detail(rows[0]))
        label, _icon = self.ui._app_primary_action(rows[0])
        self.assertTrue(label.startswith("Install "))
        installed_label, _ = self.ui._app_primary_action(rows[1])
        self.assertEqual(installed_label, "")


class TestNoShadowedHelpers(unittest.TestCase):
    """A nested def silently shadows a module-level function of the same name.

    This is parsed rather than imported so it runs without GTK.

    It is not a style rule. `run_health_scan` existed at module level and again
    as a nested UI handler with an incompatible signature; the nested one won,
    every call raised TypeError inside a worker thread, and because the thread
    died before clearing its in-flight flag the health page span forever and
    every later scan was refused as "a scan is already running".
    """

    @staticmethod
    def _signature(node: ast.FunctionDef) -> tuple:
        return (
            [a.arg for a in node.args.args],
            [a.arg for a in node.args.kwonlyargs],
        )

    def test_no_module_function_is_shadowed_by_a_nested_def(self) -> None:
        tree = ast.parse((CORE / "ui.py").read_text(encoding="utf-8"))
        module_funcs = {
            n.name: n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        clashes = []

        def walk(node, chain: list[str]) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if chain and child.name in module_funcs:
                        outer = module_funcs[child.name]
                        clashes.append(
                            f"{child.name}: module line {outer.lineno} "
                            f"(args {self._signature(outer)}) shadowed at line "
                            f"{child.lineno} in {'.'.join(chain)} "
                            f"(args {self._signature(child)})"
                        )
                    walk(child, [*chain, child.name])
                else:
                    walk(child, chain)

        walk(tree, [])
        self.assertEqual(clashes, [], "shadowed helper(s):\n  " + "\n  ".join(clashes))


class TestScanWorkersAlwaysClearInflight(unittest.TestCase):
    """A worker thread that dies must not leave a scan flag set forever."""

    def test_every_scan_worker_schedules_its_completion_in_a_finally(self) -> None:
        tree = ast.parse((CORE / "ui.py").read_text(encoding="utf-8"))
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not node.name.endswith("work"):
                continue
            # The worker must schedule done() from a finally block.
            schedules_in_finally = any(
                isinstance(t, ast.Try)
                and any(
                    isinstance(c, ast.Call)
                    and getattr(c.func, "attr", "") == "idle_add"
                    for stmt in t.finalbody
                    for c in ast.walk(stmt)
                )
                for t in ast.walk(node)
            )
            calls_idle_add = any(
                isinstance(c, ast.Call) and getattr(c.func, "attr", "") == "idle_add"
                for c in ast.walk(node)
            )
            if calls_idle_add and not schedules_in_finally:
                offenders.append(f"{node.name} at line {node.lineno}")
        self.assertEqual(
            offenders,
            [],
            "worker(s) that leak an in-flight flag when they raise:\n  "
            + "\n  ".join(offenders),
        )


class TestStyleSheet(unittest.TestCase):
    """The CSS lives in a real stylesheet rather than a string in ui.py."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.ui = load_ui_module()
        except Exception as exc:
            raise unittest.SkipTest(f"GTK/libadwaita unavailable: {exc}") from exc

    def test_stylesheet_ships_with_the_app(self) -> None:
        self.assertTrue(
            self.ui.STYLE_SHEET.is_file(),
            f"missing stylesheet at {self.ui.STYLE_SHEET}",
        )

    def test_gtk_parses_the_stylesheet_without_errors(self) -> None:
        """GTK ignores properties it does not understand, so a typo or a
        GTK3-only property silently drops the styling it was meant to apply."""
        try:
            import gi

            gi.require_version("Gtk", "4.0")
            from gi.repository import Gtk
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"GTK unavailable: {exc}") from exc

        errors: list[str] = []
        provider = Gtk.CssProvider()
        provider.connect("parsing-error", lambda _p, _s, err: errors.append(err.message))
        provider.load_from_data(self.ui.STYLE_SHEET.read_bytes())
        self.assertEqual(errors, [])

    def test_load_css_survives_a_missing_stylesheet(self) -> None:
        """An unstyled window is still usable; a crash at startup is not."""
        original = self.ui.STYLE_SHEET
        try:
            self.ui.STYLE_SHEET = Path("/nonexistent/urstack/style.css")
            self.ui.load_css()
        finally:
            self.ui.STYLE_SHEET = original


if __name__ == "__main__":
    unittest.main()
