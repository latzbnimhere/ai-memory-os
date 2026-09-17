"""Regression tests for the promotion zero-open-session gate (isolated temp root; never touches ~/AI-Memory)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import promote  # noqa: E402


class TestZeroOpenSessionGate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aimem-promote-gate-"))
        self.root = self.tmp / "AI-Memory"
        self.root.mkdir()
        shutil.copytree(REPO / "bin", self.root / "bin")
        shutil.copytree(REPO / "lib", self.root / "lib", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(self.root))
        self.aimem("init", str(self.root), "--force")
        self.aimem("register", "gate", "--name", "Gate")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def aimem(self, *args):
        r = subprocess.run([sys.executable, str(self.root / "bin" / "aimem"), *args], env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout

    def promotion(self):
        return promote.Promotion(self.root, live=False, backup_dir=self.tmp / "backups")

    def test_empty_session_set_allows_gate(self):
        pr = self.promotion()
        pr.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_TEST")
        self.assertEqual(pr.report["gates"]["ZERO_OPEN_SESSIONS_TEST"]["result"], "PASS")

    def test_one_open_session_blocks_gate_without_touching_it(self):
        out = self.aimem("begin", "gate", "--agent", "codex", "--task", "owner work in progress", "--tokens", "1500")
        sid = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("SESSION_ID="))
        pr = self.promotion()
        with self.assertRaises(RuntimeError):
            pr.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_TEST")
        self.assertEqual(pr.report["gates"]["ZERO_OPEN_SESSIONS_TEST"]["result"], "FAIL")
        self.assertIn(sid, pr.report["gates"]["ZERO_OPEN_SESSIONS_TEST"]["detail"])
        # the gate never closes, supersedes or re-attributes the owner's session
        self.assertIn(f"{sid}\tOPEN", self.aimem("sessions", "--open"))

    def test_rollback_before_first_mutation_leaves_root_untouched(self):
        pr = self.promotion()
        pr.rollback("gate failed before preserve")
        self.assertTrue((self.root / "lib" / "aimem" / "core.py").exists())
        self.assertFalse((self.root / ".previous").exists())
        self.assertEqual(pr.report["rollback"]["actions"], ["NO_MUTATION_BEFORE_FAILURE"])


if __name__ == "__main__":
    unittest.main()
