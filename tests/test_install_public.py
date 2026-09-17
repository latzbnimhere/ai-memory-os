from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "tools" / "install.py"


class TestFreshInstall(unittest.TestCase):
    def run_install(self, *args):
        return subprocess.run(
            [sys.executable, str(INSTALLER), *map(str, args)],
            cwd=REPO,
            capture_output=True,
            text=True,
        )

    def test_requires_explicit_root(self):
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--root", result.stderr)

    def test_refuses_existing_target_without_touching_it(self):
        with tempfile.TemporaryDirectory(prefix="aimem-install-existing-") as temp:
            root = Path(temp) / "AI-Memory"
            root.mkdir()
            sentinel = root / "KEEP.txt"
            sentinel.write_text("UNCHANGED\n", encoding="utf-8")
            result = self.run_install("--root", root)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "UNCHANGED\n")
            self.assertIn("target already exists", result.stderr)

    def test_refuses_target_inside_source_tree(self):
        root = REPO / ".install-test-runtime"
        if root.exists():
            self.fail(f"unexpected pre-existing test path: {root}")
        result = self.run_install("--root", root)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(root.exists())
        self.assertIn("outside the source repository", result.stderr)

    def test_fresh_install_and_doctor(self):
        with tempfile.TemporaryDirectory(prefix="aimem-install-parent-") as temp:
            root = Path(temp) / "AI-Memory"
            result = self.run_install("--root", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("INSTALL=PASS", result.stdout)
            self.assertTrue((root / "bin" / "aimem").is_file())
            self.assertTrue((root / "lib" / "aimem" / "core.py").is_file())
            self.assertTrue((root / "registry" / "projects.json").is_file())
            self.assertTrue((root / "AI_MEMORY_AGENT_PROTOCOL_V4.md").is_file())
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8").strip(), "4.1.0")

            env = dict(os.environ, AI_MEMORY_ROOT=str(root))
            doctor = subprocess.run(
                [sys.executable, str(root / "bin" / "aimem"), "doctor", "--deep", "--no-repo"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)


if __name__ == "__main__":
    unittest.main()
