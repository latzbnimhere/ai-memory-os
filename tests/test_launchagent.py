"""The sweep LaunchAgent is found by what it runs, not by its label (isolated temp roots and HOME; never reads the
owner's LaunchAgents, never calls launchctl for real in the promote tests).

Installations older than the open-source release run <root>/bin/aimem-sweep under their own label; 4.2 looked only
for io.aimemory.sweep, so a live promotion of such an installation would fail LAUNCHAGENT_LOADED and roll back.
"""
from __future__ import annotations

import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import promote  # noqa: E402
from aimem import launchagent  # noqa: E402

LEGACY = "com.example.aimemory.sweep"


class _AgentsCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aimem-launchagent-"))
        self.home = self.tmp / "home"
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.root = self.tmp / "AI-Memory"
        (self.root / "bin").mkdir(parents=True)
        (self.root / "bin" / "aimem-sweep").write_text("#!/usr/bin/env python3\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def plist(self, label, target=None, filename=None, key="ProgramArguments"):
        target = str(target if target is not None else self.root / "bin" / "aimem-sweep")
        body = {"Label": label, "StartInterval": 120}
        body[key] = ["/usr/bin/python3", target] if key == "ProgramArguments" else target
        p = self.agents / (filename or f"{label}.plist")
        p.write_bytes(plistlib.dumps(body))
        return p


class TestFind(_AgentsCase):
    def test_legacy_label_running_this_roots_sweep_is_found(self):
        p = self.plist(LEGACY)
        self.assertEqual(launchagent.find(self.root, home=self.home), (LEGACY, p))

    def test_program_key_is_honoured(self):
        p = self.plist(LEGACY, key="Program")
        self.assertEqual(launchagent.find(self.root, home=self.home), (LEGACY, p))

    def test_root_reached_through_a_symlink_still_matches(self):
        link = self.tmp / "link-root"
        os.symlink(self.root, link)
        p = self.plist(LEGACY, target=link / "bin" / "aimem-sweep")
        self.assertEqual(launchagent.find(self.root, home=self.home), (LEGACY, p))

    def test_agent_of_another_root_is_ignored(self):
        self.plist(LEGACY, target=self.tmp / "other-root" / "bin" / "aimem-sweep")
        self.assertEqual(launchagent.find(self.root, home=self.home),
                         (launchagent.DEFAULT_LABEL, self.agents / "io.aimemory.sweep.plist"))

    def test_default_when_nothing_is_installed(self):
        self.assertEqual(launchagent.find(self.root, home=self.home),
                         (launchagent.DEFAULT_LABEL, self.agents / "io.aimemory.sweep.plist"))

    def test_default_label_wins_among_several_matches(self):
        self.plist(LEGACY, filename="a-first.plist")
        p = self.plist(launchagent.DEFAULT_LABEL)
        self.assertEqual(launchagent.find(self.root, home=self.home), (launchagent.DEFAULT_LABEL, p))

    def test_unreadable_and_foreign_plists_are_skipped(self):
        (self.agents / "broken.plist").write_text("not a plist")
        (self.agents / "list.plist").write_bytes(plistlib.dumps(["not", "a", "dict"]))
        (self.agents / "nolabel.plist").write_bytes(plistlib.dumps({"ProgramArguments": [str(self.root / "bin" / "aimem-sweep")]}))
        p = self.plist(LEGACY)
        self.assertEqual(launchagent.find(self.root, home=self.home), (LEGACY, p))

    def test_missing_agents_directory(self):
        shutil.rmtree(self.agents)
        self.assertEqual(launchagent.find(self.root, home=self.home)[0], launchagent.DEFAULT_LABEL)

    def test_is_loaded_matches_the_label_exactly(self):
        out = "PID\tStatus\tLabel\n-\t0\tcom.example.io.aimemory.sweep\n412\t0\tcom.apple.geod\n"
        self.assertFalse(launchagent.is_loaded("io.aimemory.sweep", out))
        self.assertTrue(launchagent.is_loaded("com.example.io.aimemory.sweep", out))
        self.assertFalse(launchagent.is_loaded(LEGACY, ""))

    def test_promote_loads_the_same_module_by_path(self):
        la = promote._launchagent_module()
        p = self.plist(LEGACY)
        self.assertEqual(la.find(self.root, home=self.home), (LEGACY, p))
        self.assertEqual(la.DEFAULT_LABEL, launchagent.DEFAULT_LABEL)


class TestPromoteIntegrateLive(_AgentsCase):
    """integrate_live() with every external command stubbed: nothing real is kickstarted or rewritten."""

    def integrate(self, loaded_labels):
        calls = []

        def fake_run(cmd, env=None, timeout=900, check=False):
            calls.append([str(c) for c in cmd])
            if "integrate-global" in cmd:
                return 0, "a: verified=True\nb: verified=True\n"
            if cmd[:2] == ["launchctl", "kickstart"]:
                return 0, ""
            if cmd == ["launchctl", "list"]:
                return 0, "PID\tStatus\tLabel\n" + "".join(f"-\t0\t{l}\n" for l in loaded_labels)
            if str(cmd[-1]).endswith("aimem-sweep"):
                return 0, "SWEEP=PASS changed_projects=0\n"
            raise AssertionError(f"unexpected command {cmd}")

        pr = promote.Promotion(self.root, live=True, backup_dir=self.tmp / "backups")
        with mock.patch.object(promote, "run", fake_run), mock.patch.object(promote.time, "sleep"), \
                mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            try:
                pr.integrate_live()
                err = None
            except RuntimeError as e:
                err = e
        return pr, calls, err

    def test_legacy_label_is_kickstarted_and_passes_the_loaded_gate(self):
        p = self.plist(LEGACY)
        pr, calls, err = self.integrate([LEGACY, "com.apple.geod"])
        self.assertIsNone(err)
        self.assertIn(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LEGACY}"], calls)
        self.assertEqual(pr.report["gates"]["LAUNCHAGENT_LOADED"]["result"], "PASS")
        self.assertEqual(pr.report["launchagent"], {"label": LEGACY, "plist": str(p)})

    def test_found_agent_not_loaded_fails_closed(self):
        self.plist(LEGACY)
        pr, _, err = self.integrate(["com.apple.geod"])
        self.assertIsNotNone(err)
        self.assertEqual(pr.report["gates"]["LAUNCHAGENT_LOADED"]["result"], "FAIL")

    def test_no_agent_for_this_root_fails_closed(self):
        self.plist(LEGACY, target=self.tmp / "other-root" / "bin" / "aimem-sweep")
        pr, calls, err = self.integrate([LEGACY])
        self.assertIsNotNone(err)
        self.assertIn(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{launchagent.DEFAULT_LABEL}"], calls)
        self.assertEqual(pr.report["gates"]["LAUNCHAGENT_LOADED"]["result"], "FAIL")

    def test_label_is_not_matched_as_a_substring(self):
        self.plist(launchagent.DEFAULT_LABEL)
        pr, _, err = self.integrate(["com.example.io.aimemory.sweep"])
        self.assertIsNotNone(err)
        self.assertEqual(pr.report["gates"]["LAUNCHAGENT_LOADED"]["result"], "FAIL")


class TestDoctorAndHealthFindLegacyAgent(_AgentsCase):
    """`aimem doctor` / `aimem health` on a throwaway root with a private HOME holding the agent plist."""

    def setUp(self):
        super().setUp()
        shutil.rmtree(self.root)
        self.root.mkdir()
        shutil.copytree(REPO / "bin", self.root / "bin")
        shutil.copytree(REPO / "lib", self.root / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(self.root), HOME=str(self.home))
        self.aimem("init", str(self.root), "--force")

    def aimem(self, *args, ok=(0,)):
        r = subprocess.run([sys.executable, str(self.root / "bin" / "aimem"), *args], env=self.env,
                           capture_output=True, text=True)
        self.assertIn(r.returncode, ok, r.stdout + r.stderr)
        return r.stdout + r.stderr

    def test_doctor_sees_the_legacy_agent(self):
        self.assertIn("LaunchAgent not installed (optional)", self.aimem("doctor"))
        self.plist(LEGACY)
        out = self.aimem("doctor")
        self.assertNotIn("LaunchAgent not installed", out)
        self.assertNotIn("LaunchAgent target missing", out)

    def test_doctor_reports_a_legacy_agent_whose_target_is_gone(self):
        self.plist(LEGACY)
        (self.root / "bin" / "aimem-sweep").unlink()
        self.assertIn("LaunchAgent target missing", self.aimem("doctor", ok=(0, 1, 2)))

    def test_health_sees_the_legacy_agent(self):
        self.assertIn("launchagent=NOT_INSTALLED", self.aimem("health", "--verbose", ok=(0, 1, 2)))
        self.plist(LEGACY)
        # the throwaway label is never loaded in launchd, so the agent is found but reported as not loaded
        self.assertIn("launchagent=INSTALLED_NOT_LOADED", self.aimem("health", "--verbose", ok=(0, 1, 2)))


if __name__ == "__main__":
    unittest.main()
