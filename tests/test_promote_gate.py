"""Regression tests for the promotion zero-open-session gate (isolated temp root; never touches ~/AI-Memory)."""
from __future__ import annotations

import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import json
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


class TestPromotionReviewFindings(TestZeroOpenSessionGate):
    """Regressions from the final adversarial review of tools/promote.py (all on throwaway roots)."""

    def tree_fingerprint(self):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in (".locks", ".run")]
            for fn in filenames:
                f = Path(dirpath) / fn
                out[str(f.relative_to(self.root))] = f.read_bytes()
        return out

    def test_unknown_smoke_slug_fails_before_any_mutation(self):
        pr = promote.Promotion(self.root, live=False, backup_dir=self.tmp / "backups", smoke_slug="no-such-project")
        before = self.tree_fingerprint()
        with self.assertRaises(RuntimeError):
            pr.smoke_slug_gate()
        self.assertEqual(pr.report["gates"]["SMOKE_SLUG_REGISTERED"]["result"], "FAIL")
        self.assertEqual(self.tree_fingerprint(), before)
        pr2 = promote.Promotion(self.root, live=False, backup_dir=self.tmp / "backups", smoke_slug="gate")
        pr2.smoke_slug_gate()

    def test_newer_target_root_is_refused_before_install(self):
        (self.root / "VERSION").write_text("9.9.9\n")
        before = self.tree_fingerprint()
        pr = self.promotion()
        with self.assertRaises(RuntimeError):
            pr.migrate_precheck_gate()
        self.assertIn("MIGRATE_REFUSED_NEWER_DATA", pr.report["gates"]["NEW_ENGINE_MIGRATE_PRECHECK"]["detail"])
        self.assertEqual(self.tree_fingerprint(), before, "the precheck is a read-only dry-run")

    def test_rollback_closes_the_smoke_session(self):
        pr = promote.Promotion(self.root, live=False, backup_dir=self.tmp / "backups", smoke_slug="gate")
        pr.preserve()
        out = self.aimem("begin", "gate", "--agent", "other", "--task", "promotion smoke test", "--tokens", "1500")
        pr.smoke_sid = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("SESSION_ID="))
        pr.rollback("SMOKE_FINISH_ADMIN failed")
        self.assertTrue(pr.report["rollback"]["smoke_session_closed"])
        self.assertEqual(self.aimem("sessions", "--open").strip(), "")

    def test_rehearsal_copy_cannot_reach_live_data(self):
        live = self.tmp / "live"
        ext_dir = self.tmp / "external-cold"
        ext_file = self.tmp / "external-note.md"
        (live / "registry").mkdir(parents=True)
        (live / "projects" / "p").mkdir(parents=True)
        (live / "registry" / "handoffs.json").write_text('{"version": 1, "projects": []}')
        (live / ".handoff").mkdir()
        (live / "config.json").write_text('{"version": 4}')
        (live / "projects" / "p" / "CURRENT.md").write_text("cur")
        ext_dir.mkdir()
        (ext_dir / "journal.jsonl").write_text('{"n": 1}\n')
        ext_file.write_text("external")
        os.symlink(ext_dir, live / "projects" / "p" / "cold")
        os.symlink(ext_file, live / "projects" / "p" / "note.md")
        os.symlink(live / "projects" / "p" / "CURRENT.md", live / "projects" / "p" / "abs-inroot.md")
        os.symlink(self.tmp / "missing", live / "projects" / "p" / "dangling")
        copy = self.tmp / "rehearsal" / "AI-Memory"
        shutil.copytree(live, copy, symlinks=True)
        stats = promote._isolate_copy(live, copy, self.tmp / "rehearsal")
        self.assertTrue(stats["handoff_bridge_disabled"])
        self.assertFalse((copy / "registry" / "handoffs.json").exists())
        self.assertFalse((copy / ".handoff").exists())
        self.assertTrue(str(json.loads((copy / "config.json").read_text())["backup_dir"]).startswith(str(self.tmp / "rehearsal")))
        self.assertEqual((stats["links_rewritten"], stats["links_dereferenced"], stats["links_removed"]), (1, 2, 1))
        inroot = copy / "projects" / "p" / "abs-inroot.md"
        self.assertTrue(os.path.realpath(inroot).startswith(os.path.realpath(copy)))
        for p_ in (copy / "projects" / "p" / "cold", copy / "projects" / "p" / "note.md"):
            self.assertFalse(p_.is_symlink())
        with open(copy / "projects" / "p" / "cold" / "journal.jsonl", "a") as fh:
            fh.write('{"n": 2}\n')
        (copy / "projects" / "p" / "note.md").write_text("changed in copy")
        self.assertEqual((ext_dir / "journal.jsonl").read_text(), '{"n": 1}\n')
        self.assertEqual(ext_file.read_text(), "external")
        self.assertTrue((live / "registry" / "handoffs.json").exists(), "the live root itself is never modified")

    def test_live_change_detection(self):
        before = {"projects/p/CURRENT.md": (3, 1, 1, 0o100644), "projects/p/EVENTS.jsonl": (10, 1, 2, 0o100644),
                  "projects/p/AUTO_PHYSICAL_STATE.json": (5, 1, 3, 0o100644)}
        sweep_only = dict(before, **{"projects/p/EVENTS.jsonl": (20, 2, 2, 0o100644),
                                     "projects/p/AUTO_PHYSICAL_STATE.json": (6, 2, 3, 0o100644)})
        self.assertEqual(promote._live_changes(before, sweep_only), [])
        noise = dict(before, **{".DS_Store": (6, 1, 9, 0o100644), "projects/p/.AUTO_PHYSICAL_STATE.json.x1.tmp": (1, 1, 8, 0o100600)})
        self.assertEqual(promote._live_changes(before, noise), [])
        touched = dict(before, **{"projects/p/CURRENT.md": (4, 2, 1, 0o100644)})
        self.assertEqual(promote._live_changes(before, touched), ["projects/p/CURRENT.md: modified"])
        shrunk = dict(before, **{"projects/p/EVENTS.jsonl": (5, 2, 2, 0o100644)})
        self.assertEqual(promote._live_changes(before, shrunk), ["projects/p/EVENTS.jsonl: modified"])


if __name__ == "__main__":
    unittest.main()
