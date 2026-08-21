#!/usr/bin/env python3
"""
Tests for cancelling a long-running job.

Two things have to hold for a cancel to be safe:

1. The whole job stops. A job is a shell script that shells out to rsync and
   tar, so killing only the script would leave those copying in the background.

2. Whatever it left behind is unusable. A backup or restore point that stopped
   partway is a partial tree; restoring one applies the steps that happened to
   finish and skips the rest, which is indistinguishable from success.

Only jobs that copy files may be cancellable. Anything driving a package
transaction must not be, so the last test pins that down.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "lib" / "core" / "ui.py"
COMMON = ROOT / "lib" / "core" / "common.sh"
BACKUP = ROOT / "lib" / "plugins" / "backup.sh"
HEALTH = ROOT / "lib" / "plugins" / "health.sh"

# Headless: the refusal paths call _fs_err, which reaches for zenity when a
# display is present and blocks the test on a dialog.
HEADLESS = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}


def run_bash(body: str, *, sources: list[Path], env: dict | None = None) -> subprocess.CompletedProcess:
    script = "".join(f"source {s}\n" for s in sources) + body
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={**HEADLESS, **(env or {})},
    )


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestIncompleteBackup(unittest.TestCase):
    """A backup that stopped partway must not be restorable."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.bp = Path(self._dir.name) / "fedora-setup-test"
        (self.bp / "manifests").mkdir(parents=True)
        (self.bp / "manifests" / "dnf-user-packages.txt").write_text("firefox\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def mark(self) -> None:
        run_bash(f'mark_tree_incomplete "{self.bp}" "backup"', sources=[COMMON])

    def test_restore_refuses_an_incomplete_backup(self) -> None:
        self.mark()
        p = run_bash(f'fedora_setup_restore_from "{self.bp}"', sources=[COMMON, BACKUP])
        self.assertEqual(p.returncode, 1, "an unfinished backup must not restore")
        self.assertIn("never finished", p.stdout + p.stderr)

    def test_a_finished_backup_passes_the_gate(self) -> None:
        """Guard against the marker being left behind on the success path."""
        run_bash(f'mark_tree_incomplete "{self.bp}" x; mark_tree_complete "{self.bp}"', sources=[COMMON])
        p = run_bash(f'tree_is_incomplete "{self.bp}"', sources=[COMMON])
        self.assertEqual(p.returncode, 1, "a completed backup must not look incomplete")

    def test_marker_is_not_checksummed(self) -> None:
        """The marker outlives the manifest, so checksumming it fails every restore.

        It is written before the first step and removed after the last, which is
        after the manifest is built. If it were in the manifest, every restore
        would report the blueprint as tampered with.
        """
        self.mark()
        p = run_bash(f'_write_backup_manifest "{self.bp}"', sources=[COMMON, BACKUP])
        self.assertEqual(p.returncode, 0, p.stderr)
        manifest = (self.bp / "MANIFEST.sha256").read_text(encoding="utf-8")
        self.assertNotIn(".INCOMPLETE", manifest)

        # Removing it, as the success path does, must leave the tree verifiable.
        run_bash(f'mark_tree_complete "{self.bp}"', sources=[COMMON])
        p = run_bash(
            f'r=$(mktemp); _verify_backup_manifest "{self.bp}" "$r"; rc=$?; cat "$r"; exit $rc',
            sources=[COMMON, BACKUP],
        )
        self.assertEqual(p.returncode, 0, f"manifest should verify: {p.stdout}")


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestIncompleteRestorePoint(unittest.TestCase):
    """An interrupted restore point sorts newest but must never be chosen."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.state = Path(self._dir.name)
        self.root = self.state / "stackup" / "health-restore-points"
        self.good = self.root / "20200101-000000"
        self.bad = self.root / "29990101-000000"
        for d in (self.good, self.bad):
            (d / "files").mkdir(parents=True)
            (d / "state").mkdir(parents=True)
        (self.good / "meta.conf").write_text("id=20200101-000000\ncreated=x\nreason=good\n", encoding="utf-8")
        run_bash(f'mark_tree_incomplete "{self.bad}" "restore point"', sources=[COMMON])

    def tearDown(self) -> None:
        self._dir.cleanup()

    def health(self, body: str) -> subprocess.CompletedProcess:
        return run_bash(body, sources=[COMMON, HEALTH], env={"XDG_STATE_HOME": str(self.state)})

    def test_latest_skips_the_interrupted_point(self) -> None:
        p = self.health("fedora_health_restore_point_latest")
        self.assertEqual(
            p.stdout.strip(),
            "20200101-000000",
            "an unusable newest point would hide the last good one",
        )

    def test_apply_refuses_the_interrupted_point(self) -> None:
        p = self.health('fedora_health_restore_point_apply "29990101-000000"')
        self.assertEqual(p.returncode, 1)
        self.assertIn("never finished", p.stdout + p.stderr)

    def test_list_says_why_it_is_unusable(self) -> None:
        p = self.health("fedora_health_restore_point_list")
        rows = {ln.split("|")[0]: ln for ln in p.stdout.strip().splitlines()}
        self.assertIn("incomplete", rows["29990101-000000"])
        self.assertNotIn("incomplete", rows["20200101-000000"])


class TestTerminateProcessGroup(unittest.TestCase):
    """Killing the job script alone would leave rsync and tar running."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("urstack_ui_cancel", UI)
        cls.ui = importlib.util.module_from_spec(spec)
        sys.modules["urstack_ui_cancel"] = cls.ui
        try:
            spec.loader.exec_module(cls.ui)
        except Exception as exc:
            raise unittest.SkipTest(f"GTK unavailable: {exc}") from exc

    def test_children_die_with_the_job(self) -> None:
        # A script that outlives its parent unless the group is signalled.
        proc = subprocess.Popen(
            ["bash", "-c", "sleep 120 & sleep 120 & wait"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)
        deadline = time.time() + 10
        while time.time() < deadline:
            if len(subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout.split()) >= 3:
                break
            time.sleep(0.1)
        before = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout.split()
        self.assertGreaterEqual(len(before), 3, "expected a parent and two children")

        self.ui.terminate_process_group(proc)
        time.sleep(0.5)

        after = subprocess.run(["pgrep", "-g", str(pgid)], capture_output=True, text=True).stdout.split()
        self.assertEqual(after, [], f"survivors kept running: {after}")

    def test_already_dead_process_is_harmless(self) -> None:
        proc = subprocess.Popen(["true"], start_new_session=True)
        proc.wait()
        self.ui.terminate_process_group(proc)  # must not raise
        self.ui.terminate_process_group(None)


class TestOnlyFileCopyingJobsAreCancellable(unittest.TestCase):
    """Interrupting a package transaction can leave the RPM database broken.

    Cancel is therefore restricted to jobs that only copy files out. This reads
    the call sites so adding a cancellable dnf job fails here rather than in
    front of a user mid-upgrade.
    """

    ALLOWED: ClassVar[set[str]] = {"Backup", "Creating restore point"}

    def test_no_transactional_job_offers_cancel(self) -> None:
        tree = ast.parse(UI.read_text(encoding="utf-8"))
        cancellable, seen = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", "") != "run_embedded_job":
                continue
            kw = {k.arg: k.value for k in node.keywords}
            title = kw.get("title")
            title = title.value if isinstance(title, ast.Constant) else f"<line {node.lineno}>"
            seen.append(title)
            flag = kw.get("cancellable")
            if isinstance(flag, ast.Constant) and flag.value is True:
                cancellable.append(title)

        self.assertTrue(seen, "no run_embedded_job call sites found -- test is stale")
        unexpected = sorted(set(cancellable) - self.ALLOWED)
        self.assertEqual(unexpected, [], f"these must not be interruptible: {unexpected}")
        self.assertIn("Backup", cancellable, "the backup job should offer Cancel")


if __name__ == "__main__":
    unittest.main()
