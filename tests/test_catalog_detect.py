#!/usr/bin/env python3
"""
Installed-detection tests for catalog_status_file.

The catalog only records the install method it *prefers*, but a user may have the
same app from another source (Okular is catalogued as the org.kde.okular Flatpak
yet ships as an RPM on Fedora KDE). These tests drive the real shell function with
stubbed rpm/flatpak/snap so both directions are covered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "lib" / "core"

STUB = """#!/usr/bin/env bash
cat <<'EOF'
{payload}
EOF
"""


class DetectHarness:
    """A throwaway machine: fake catalog, package managers and desktop entries."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.bin = tmp / "bin"
        self.home = tmp / "home"
        self.catalog = tmp / "data" / "catalog"
        for d in (self.bin, self.catalog, self.home / ".local/share/applications"):
            d.mkdir(parents=True, exist_ok=True)

    def _stub(self, name: str, lines: list[str]) -> None:
        p = self.bin / name
        p.write_text(STUB.format(payload="\n".join(lines)), encoding="utf-8")
        p.chmod(0o755)

    def setup(
        self,
        apps: list[dict],
        rpms: list[str] | None = None,
        flatpaks: list[str] | None = None,
        snaps: list[str] | None = None,
        desktop: list[str] | None = None,
    ) -> None:
        (self.catalog / "apps.json").write_text(
            json.dumps({"version": 1, "categories": [{"id": "utilities", "name": "Utilities", "apps": apps}]}),
            encoding="utf-8",
        )
        self._stub("rpm", rpms or [])
        self._stub("flatpak", flatpaks or [])
        # `snap list` output has a header row that the parser drops.
        self._stub("snap", ["Name Version Rev Tracking Publisher Notes", *(snaps or [])])
        for entry in desktop or []:
            (self.home / ".local/share/applications" / f"{entry}.desktop").write_text(
                "[Desktop Entry]\nType=Application\n", encoding="utf-8"
            )

    def run(self) -> dict[str, str]:
        out = self.tmp / "status.txt"
        env = {
            **os.environ,
            "HOME": str(self.home),
            "STACKUP_ROOT": str(self.tmp),
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "XDG_DATA_DIRS": "",
        }
        subprocess.run(
            ["bash", "-c", f'source "{CORE}/catalog.sh"; catalog_status_file "{out}"'],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        result = {}
        for line in out.read_text(encoding="utf-8").splitlines():
            parts = line.split("|")
            if len(parts) > 7:
                result[parts[0]] = parts[7]
        return result


def app(aid: str, method: str, package: str, name: str = "") -> dict:
    return {"id": aid, "name": name or aid, "summary": "", "method": method, "package": package}


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestCrossSourceDetection(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.h = DetectHarness(Path(self._dir.name))

    def tearDown(self) -> None:
        self._dir.cleanup()

    def detect(self, **kw) -> dict[str, str]:
        self.h.setup(**kw)
        return self.h.run()

    # --- the original bug ---------------------------------------------------

    def test_flatpak_app_installed_as_rpm_is_detected(self) -> None:
        """The Okular case: catalogued as a Flatpak, present as an RPM."""
        got = self.detect(
            apps=[app("okular", "flatpak", "org.kde.okular")],
            rpms=["okular"],
            desktop=["org.kde.okular"],
        )
        self.assertEqual(got["okular"], "1")

    def test_flatpak_app_genuinely_absent(self) -> None:
        # Fictional id: the detector also reads the real /usr/share/applications,
        # so a negative case has to name something no machine can have.
        got = self.detect(apps=[app("zzabsent", "flatpak", "org.example.Zzabsentapp")])
        self.assertEqual(got["zzabsent"], "0")

    def test_dnf_app_installed_as_flatpak_is_detected(self) -> None:
        """The reverse direction: catalogued for dnf, installed from Flathub."""
        got = self.detect(
            apps=[app("gimp", "dnf", "gimp")],
            flatpaks=["org.gimp.GIMP"],
        )
        self.assertEqual(got["gimp"], "1")

    # --- false-positive guards ---------------------------------------------

    def test_generic_tail_does_not_match_across_apps(self) -> None:
        """org.telegram.desktop must not match because some *other* .Desktop app exists."""
        got = self.detect(
            apps=[app("telegram", "flatpak", "org.telegram.desktop")],
            flatpaks=["io.github.shiftey.Desktop"],
        )
        self.assertEqual(got["telegram"], "0")

    def test_unrelated_cli_sharing_a_name_is_not_the_gui_app(self) -> None:
        """The real case is `boxes` the ASCII-art RPM vs org.gnome.Boxes: an RPM
        of the same short name, with no desktop entry, is not the GUI app."""
        got = self.detect(
            apps=[app("zzwidget", "flatpak", "org.example.Zzwidget")],
            rpms=["zzwidget"],
        )
        self.assertEqual(got["zzwidget"], "0")

    def test_same_name_with_a_desktop_entry_is_the_gui_app(self) -> None:
        got = self.detect(
            apps=[app("zzwidget", "flatpak", "org.example.Zzwidget")],
            rpms=["zzwidget"],
            desktop=["org.example.Zzwidget"],
        )
        self.assertEqual(got["zzwidget"], "1")

    def test_two_component_id_does_not_yield_a_generic_tail(self) -> None:
        """`battle.net` must not be reduced to the very common name `net`."""
        got = self.detect(
            apps=[app("battlenet", "browser", "battle.net")],
            rpms=["net", "net-tools"],
            desktop=["net"],
        )
        self.assertEqual(got["battlenet"], "0")

    # --- name-shape handling ------------------------------------------------

    def test_snap_under_vendor_name(self) -> None:
        """com.spotify.Client is just `spotify` as a snap."""
        got = self.detect(
            apps=[app("spotify", "flatpak", "com.spotify.Client")],
            snaps=["spotify 1.2 99 latest/stable spotify -"],
        )
        self.assertEqual(got["spotify"], "1")

    def test_snap_under_vendor_and_tail(self) -> None:
        got = self.detect(
            apps=[app("onlyoffice", "flatpak", "org.onlyoffice.desktopeditors")],
            snaps=["onlyoffice-desktopeditors 9.4 1 latest/stable onlyoffice -"],
        )
        self.assertEqual(got["onlyoffice"], "1")

    def test_split_rpm_family_with_no_bare_package(self) -> None:
        """Fedora has libreoffice-core but no `libreoffice` package."""
        got = self.detect(
            apps=[app("libreoffice", "flatpak", "org.libreoffice.LibreOffice")],
            rpms=["libreoffice-core", "libreoffice-writer"],
            desktop=["libreoffice-startcenter"],
        )
        self.assertEqual(got["libreoffice"], "1")

    def test_rebranded_desktop_id_still_matches_rpm(self) -> None:
        """Ptyxis ships app.devsuite.Ptyxis upstream but org.gnome.Ptyxis on Fedora."""
        got = self.detect(
            apps=[app("ptyxis", "flatpak", "app.devsuite.Ptyxis")],
            rpms=["ptyxis"],
            desktop=["org.gnome.Ptyxis"],
        )
        self.assertEqual(got["ptyxis"], "1")

    def test_punctuation_differences_are_normalized(self) -> None:
        """`sublime-text` vs the com.sublimehq.SublimeText flatpak."""
        got = self.detect(
            apps=[app("sublime-text", "browser", "sublime-text")],
            flatpaks=["com.sublimehq.SublimeText"],
        )
        self.assertEqual(got["sublime-text"], "1")

    def test_browser_method_consults_other_sources(self) -> None:
        """The browser branch used to return 0 without checking anything."""
        got = self.detect(
            apps=[app("zoom", "browser", "zoom")],
            flatpaks=["us.zoom.Zoom"],
        )
        self.assertEqual(got["zoom"], "1")

    def test_exact_matches_still_work_per_method(self) -> None:
        got = self.detect(
            apps=[
                app("vlc", "flatpak", "org.videolan.VLC"),
                app("git", "dnf", "git"),
            ],
            flatpaks=["org.videolan.VLC"],
            rpms=["git"],
        )
        self.assertEqual(got["vlc"], "1")
        self.assertEqual(got["git"], "1")


if __name__ == "__main__":
    unittest.main()
