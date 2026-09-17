from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
PROMOTE = REPO / "tools" / "promote.py"


class TestPublicPromoteSafety(unittest.TestCase):
    def run_promote(self, *args):
        return subprocess.run(
            [sys.executable, str(PROMOTE), *args],
            capture_output=True,
            text=True,
        )

    def test_rehearse_requires_explicit_root(self):
        r = self.run_promote("--rehearse")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--root", r.stderr)

    def test_live_requires_explicit_root(self):
        r = self.run_promote("--live")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--root", r.stderr)

    def test_live_requires_second_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="aimem-public-promote-") as td:
            root = Path(td) / "memory"
            (root / "bin").mkdir(parents=True)
            (root / "bin" / "aimem").write_text("placeholder\n")

            sentinel = root / "SENTINEL"
            sentinel.write_text("UNCHANGED\n")

            r = self.run_promote(
                "--live",
                "--root",
                str(root),
            )

            self.assertNotEqual(r.returncode, 0)
            self.assertIn("--confirm-live-root", r.stderr)
            self.assertEqual(sentinel.read_text(), "UNCHANGED\n")
            self.assertFalse((root / ".previous").exists())

    def test_live_rejects_mismatched_confirmation(self):
        with tempfile.TemporaryDirectory(prefix="aimem-public-promote-") as td:
            root = Path(td) / "memory"
            other = Path(td) / "other"

            (root / "bin").mkdir(parents=True)
            (root / "bin" / "aimem").write_text("placeholder\n")
            other.mkdir()

            sentinel = root / "SENTINEL"
            sentinel.write_text("UNCHANGED\n")

            r = self.run_promote(
                "--live",
                "--root",
                str(root),
                "--confirm-live-root",
                str(other),
            )

            self.assertNotEqual(r.returncode, 0)
            self.assertIn("does not match", r.stderr)
            self.assertEqual(sentinel.read_text(), "UNCHANGED\n")
            self.assertFalse((root / ".previous").exists())


if __name__ == "__main__":
    unittest.main()
