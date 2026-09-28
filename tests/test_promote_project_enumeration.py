"""Regression tests for the rehearsal's project enumeration, live-root check and temp cleanup (isolated temp roots;
never touches ~/AI-Memory).

- A real rehearsal failed with NotADirectoryError on projects/.DS_Store/project.json: the canonical snapshot
  enumerated projects with Path.glob("*/"), which on Python < 3.11 also yields plain files.
- The next one failed LIVE_ROOT_UNTOUCHED_BY_REHEARSAL on logs/aimem-sweep.out.log, the launchd sweep's stdout log
  that it appends to every run; it is now accepted only as a pure append to the same file.
- Its copy was not removed: sealed (read-only) artifact directories defeated shutil.rmtree(ignore_errors=True).
"""
from __future__ import annotations

import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import promote  # noqa: E402

SWEEP_LOG = os.path.join("logs", "aimem-sweep.out.log")


class _RehearsalRootCase(unittest.TestCase):
    SLUGS = ("alpha", "beta")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aimem-promote-enum-"))
        self.root = self.tmp / "AI-Memory"
        (self.root / "registry").mkdir(parents=True)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "aimem").write_text("#!/usr/bin/env python3\n")
        (self.root / "logs").mkdir()
        self.sweep_log = self.root / SWEEP_LOG
        self.sweep_log.write_text("SWEEP=PASS changed_projects=4\nSWEEP=PASS changed_projects=0\n")
        for slug in self.SLUGS:
            self.make_project(slug)
        self.write_registry(self.SLUGS)

    def tearDown(self):
        promote._remove_tree(self.tmp)

    def make_project(self, slug, manifest=None):
        p = self.root / "projects" / slug
        (p / "checkpoints").mkdir(parents=True)
        (p / "checkpoints" / "cp-1.json").write_text("{}")
        (p / "project.json").write_text(json.dumps(manifest if manifest is not None else
                                                   {"slug": slug, "current_checkpoint": "cp-1"}))
        (p / "CURRENT.md").write_text(f"# {slug} current\n")
        (p / "NEXT.md").write_text(f"# {slug} next\n")
        (p / "EVENTS.jsonl").write_text('{"n": 1}\n{"n": 2}\n')
        return p

    def write_registry(self, slugs):
        (self.root / "registry" / "projects.json").write_text(json.dumps({"projects": {s: {} for s in slugs}}))

    def add_benign_entries(self):
        projects = self.root / "projects"
        (projects / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1" + b"\x00" * 64)
        (projects / "._alpha").write_bytes(b"\x00\x05\x16\x07")  # AppleDouble resource fork
        (projects / ".localized").write_text("")
        (projects / "README.txt").write_text("not a project\n")
        (projects / "Icon\r").write_bytes(b"")
        os.symlink(projects / "README.txt", projects / "readme-link")
        os.symlink(self.tmp / "nowhere", projects / "dangling-link")

    def add_sealed_artifacts(self):
        """Read-only artifact directories, as the live root seals finished artifacts (dr-xr-xr-x)."""
        sealed = self.root / "projects" / "alpha" / "artifacts" / "release-20260922"
        (sealed / "evidence").mkdir(parents=True)
        (sealed / "evidence" / "report.md").write_text("sealed\n")
        (sealed / "MANIFEST.json").write_text("{}")
        os.chmod(sealed / "evidence", 0o555)
        os.chmod(sealed, 0o555)

    def run_rehearse(self, promotion_rc=0, during=None, expect_removed=True):
        """rehearse() on self.root with the Promotion replaced by a stub (the real one runs this very test suite
        as a gate); `during` runs while the promotion would, e.g. to simulate the launchd sweep writing to the live
        root. Reports go to a temp DEV so the repository tree is not written."""
        dev = self.tmp / "dev"
        dev.mkdir(exist_ok=True)
        made = []

        class StubPromotion:
            def __init__(self, root, **kw):
                made.append(root)
                self.report = {"result": "PASS" if promotion_rc == 0 else "FAIL", "gates": {}}

            def run(self):
                if during:
                    during()
                return promotion_rc

        args = argparse.Namespace(skip_selftest=True, smoke_slug="alpha", keep=False)
        buf = io.StringIO()
        with mock.patch.object(promote, "Promotion", StubPromotion), mock.patch.object(promote, "DEV", dev), \
                contextlib.redirect_stdout(buf):
            rc = promote.rehearse(args, self.root)
        out = buf.getvalue()
        copy = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("REHEARSAL_COPY=")), None)
        if copy and expect_removed:
            self.assertFalse(os.path.lexists(Path(copy).parent), "the rehearsal copy is always removed")
            self.assertIn(f"REHEARSAL_TEMP_CLEANUP=PASS {Path(copy).parent}", out)
        return rc, out, made


class TestRehearsalProjectEnumeration(_RehearsalRootCase):
    def assert_fails_closed(self, fragment):
        with self.assertRaises(promote.ProjectLayoutError) as cm:
            promote._canonical_snapshot(self.root)
        self.assertIn(fragment, str(cm.exception))

    # ------------------------------------------------------------ benign entries are skipped
    def test_ds_store_alongside_valid_projects(self):
        (self.root / "projects" / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        snap = promote._canonical_snapshot(self.root)
        self.assertEqual(sorted(snap), list(self.SLUGS))
        self.assertEqual(snap["alpha"]["current_checkpoint"], "cp-1")
        self.assertEqual(snap["alpha"]["checkpoints"], ["cp-1.json"])
        self.assertEqual(snap["alpha"]["events_lines"], 2)

    def test_other_benign_non_directory_entries_are_skipped(self):
        self.add_benign_entries()
        self.assertEqual(sorted(promote._canonical_snapshot(self.root)), list(self.SLUGS))

    def test_unregistered_valid_project_is_still_compared(self):
        self.make_project("gamma")
        self.assertIn("gamma", promote._canonical_snapshot(self.root))

    # ------------------------------------------------------------ malformed projects fail closed
    def test_directory_without_project_json_fails_closed(self):
        (self.root / "projects" / "orphan").mkdir()
        (self.root / "projects" / ".DS_Store").write_bytes(b"Bud1")
        self.assert_fails_closed("orphan: project directory without a project.json file")

    def test_registered_project_missing_project_json_fails_closed(self):
        (self.root / "projects" / "beta" / "project.json").unlink()
        self.assert_fails_closed("beta: project directory without a project.json file")

    def test_project_json_that_is_a_directory_fails_closed(self):
        pj = self.root / "projects" / "beta" / "project.json"
        pj.unlink()
        pj.mkdir()
        self.assert_fails_closed("beta: project directory without a project.json file")

    def test_invalid_project_json_fails_closed(self):
        (self.root / "projects" / "alpha" / "project.json").write_text('{"slug": "alpha", ')
        self.assert_fails_closed("alpha: unreadable project.json")

    def test_non_object_project_json_fails_closed(self):
        (self.root / "projects" / "alpha" / "project.json").write_text('["alpha"]')
        self.assert_fails_closed("alpha: project.json is not a JSON object")

    def test_registered_slug_replaced_by_a_file_fails_closed(self):
        promote._remove_tree(self.root / "projects" / "beta")
        (self.root / "projects" / "beta").write_text("oops")
        self.assert_fails_closed("beta: registered but projects/beta is not a project directory")

    def test_registered_slug_without_directory_fails_closed(self):
        self.write_registry(self.SLUGS + ("ghost",))
        self.assert_fails_closed("ghost: registered but projects/ghost is not a project directory")

    def test_invalid_registry_fails_closed(self):
        (self.root / "registry" / "projects.json").write_text("{not json")
        self.assert_fails_closed("registry/projects.json unreadable")

    # ------------------------------------------------------------ rehearse() end to end (promotion stubbed)
    def test_rehearse_passes_with_ds_store_under_projects(self):
        self.add_benign_entries()
        before = promote._live_manifest(self.root)
        rc, out, made = self.run_rehearse()
        self.assertEqual(rc, 0, out)
        self.assertIn("REHEARSAL_CANONICAL_PRESERVED=True", out)
        self.assertIn("MIGRATION_REHEARSAL=PASS", out)
        self.assertEqual(len(made), 1)
        self.assertEqual(promote._live_changes(before, promote._live_manifest(self.root)), [])

    def test_rehearse_refuses_malformed_project_before_copying(self):
        (self.root / "projects" / "orphan").mkdir()
        before = promote._live_manifest(self.root)
        rc, out, made = self.run_rehearse()
        self.assertEqual(rc, 1)
        self.assertIn("REHEARSAL_REFUSED=malformed projects/ layout on the source root", out)
        self.assertIn("MIGRATION_REHEARSAL=FAIL", out)
        self.assertNotIn("REHEARSAL_COPY=", out)
        self.assertEqual(made, [], "no promotion may run against a root that fails the layout check")
        self.assertEqual(before, promote._live_manifest(self.root))


class TestRehearsalLiveRootCheck(_RehearsalRootCase):
    """logs/aimem-sweep.out.log may only grow by appending to the same file; every other path stays strict."""

    def snapshot(self):
        return promote._live_manifest(self.root), promote._append_only_snapshot(self.root)

    def check(self, snap):
        return promote._live_root_changes(self.root, *snap)

    def append(self, text="SWEEP=PASS changed_projects=1\n"):
        with open(self.sweep_log, "a") as fh:
            fh.write(text)

    def replace_log(self, data):
        new = self.sweep_log.with_name("new.log")
        new.write_bytes(data)
        os.replace(new, self.sweep_log)

    def test_unchanged_sweep_log_passes(self):
        snap = self.snapshot()
        self.assertEqual(self.check(snap), ([], [f"{SWEEP_LOG}: unchanged"]))

    def test_append_only_growth_passes_and_is_recorded(self):
        snap = self.snapshot()
        self.append()
        self.append()
        self.assertEqual(self.check(snap), ([], [f"{SWEEP_LOG}: appended 60 bytes"]))

    def test_modified_existing_prefix_fails(self):
        snap = self.snapshot()
        with open(self.sweep_log, "r+b") as fh:  # in place: same inode, same size
            fh.write(b"X")
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: existing bytes modified"], []))

    def test_modified_prefix_with_appended_tail_fails(self):
        snap = self.snapshot()
        with open(self.sweep_log, "r+b") as fh:
            fh.seek(6)
            fh.write(b"FAIL")
        self.append()
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: existing bytes modified"], []))

    def test_truncated_sweep_log_fails(self):
        snap = self.snapshot()
        os.truncate(self.sweep_log, 5)
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: truncated (60 -> 5 bytes)"], []))

    def test_truncated_then_regrown_sweep_log_fails(self):
        snap = self.snapshot()
        os.truncate(self.sweep_log, 0)
        self.append("SWEEP=PASS changed_projects=9\n" * 3)
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: existing bytes modified"], []))

    def test_removed_sweep_log_fails(self):
        snap = self.snapshot()
        self.sweep_log.unlink()
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: removed"], []))

    def test_recreated_log_without_original_prefix_fails(self):
        snap = self.snapshot()
        self.replace_log(b"SWEEP=PASS changed_projects=7\n" * 4)
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: replaced (not the same file)"], []))

    def test_recreated_log_keeping_the_prefix_still_fails(self):
        snap = self.snapshot()
        self.replace_log(self.sweep_log.read_bytes() + b"SWEEP=PASS changed_projects=1\n")
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: replaced (not the same file)"], []))

    def test_log_replaced_by_symlink_fails(self):
        snap = self.snapshot()
        target = self.tmp / "elsewhere.log"
        target.write_bytes(self.sweep_log.read_bytes() + b"more\n")
        self.sweep_log.unlink()
        os.symlink(target, self.sweep_log)
        changes, expected = self.check(snap)
        self.assertEqual(expected, [])
        self.assertEqual(len(changes), 1)
        self.assertTrue(changes[0].startswith(f"{SWEEP_LOG}: replaced or unreadable"), changes)

    def test_mode_change_fails(self):
        snap = self.snapshot()
        os.chmod(self.sweep_log, 0o666)
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: mode changed"], []))

    def test_log_created_during_rehearsal_is_not_tolerated(self):
        self.sweep_log.unlink()
        snap = self.snapshot()
        self.assertEqual(snap[1], {})
        self.append()
        self.assertEqual(self.check(snap), ([f"{SWEEP_LOG}: added"], []))

    def test_unrelated_live_root_change_fails_alongside_an_append(self):
        snap = self.snapshot()
        self.append()
        (self.root / "projects" / "alpha" / "CURRENT.md").write_text("# alpha current, rewritten\n")
        self.assertEqual(self.check(snap), ([os.path.join("projects", "alpha", "CURRENT.md") + ": modified"],
                                            [f"{SWEEP_LOG}: appended 30 bytes"]))

    def test_other_logs_are_not_append_tolerated(self):
        err = self.root / "logs" / "aimem-sweep.err.log"
        err.write_text("old\n")
        snap = self.snapshot()
        with open(err, "a") as fh:
            fh.write("new\n")
        self.assertEqual(self.check(snap)[0], [os.path.join("logs", "aimem-sweep.err.log") + ": modified"])

    # ------------------------------------------------------------ rehearse() end to end (promotion stubbed)
    def test_rehearse_passes_with_concurrent_sweep_append(self):
        rc, out, _ = self.run_rehearse(during=self.append)
        self.assertEqual(rc, 0, out)
        self.assertIn(f"LIVE_ROOT_EXPECTED_CONCURRENT_WRITES=['{SWEEP_LOG}: appended 30 bytes']", out)
        self.assertIn("LIVE_ROOT_UNTOUCHED_BY_REHEARSAL=True", out)
        self.assertIn("MIGRATION_REHEARSAL=PASS", out)
        rep = json.loads(next((self.tmp / "dev" / "reports").glob("MIGRATION_REHEARSAL_*.json")).read_text())
        self.assertEqual(rep["live_expected_concurrent_writes"], [f"{SWEEP_LOG}: appended 30 bytes"])

    def test_rehearse_fails_when_sweep_log_prefix_changes(self):
        def rewrite():
            self.sweep_log.write_text("SWEEP=FAIL rewritten history\n" * 3)
        rc, out, _ = self.run_rehearse(during=rewrite)
        self.assertEqual(rc, 1)
        self.assertIn(f"LIVE_ROOT_UNTOUCHED_BY_REHEARSAL=False CHANGES=['{SWEEP_LOG}: existing bytes modified']", out)
        self.assertIn("MIGRATION_REHEARSAL=FAIL", out)

    def test_rehearse_fails_on_unrelated_live_change(self):
        def touch():
            self.append()
            (self.root / "projects" / "beta" / "NEXT.md").write_text("changed by someone else\n")
        rc, out, _ = self.run_rehearse(during=touch)
        self.assertEqual(rc, 1)
        self.assertIn("LIVE_ROOT_UNTOUCHED_BY_REHEARSAL=False", out)
        self.assertIn(os.path.join("projects", "beta", "NEXT.md") + ": modified", out)


class TestRehearsalTempCleanup(_RehearsalRootCase):
    """The copy holds a full duplicate of private memory, incl. sealed read-only artifacts; it must always go."""

    def setUp(self):
        super().setUp()
        self.add_sealed_artifacts()

    def test_copy_with_sealed_artifacts_removed_on_success(self):
        rc, out, _ = self.run_rehearse()
        self.assertEqual(rc, 0, out)

    def test_copy_with_sealed_artifacts_removed_on_promotion_failure(self):
        rc, out, _ = self.run_rehearse(promotion_rc=1)
        self.assertEqual(rc, 1)
        self.assertIn("MIGRATION_REHEARSAL=FAIL", out)

    def test_copy_removed_when_rehearse_raises(self):
        # the first real failure raised out of rehearse() and left a full copy of the root in $TMPDIR
        args = argparse.Namespace(skip_selftest=True, smoke_slug="alpha", keep=False)
        buf = io.StringIO()
        with mock.patch.object(promote, "_isolate_copy", side_effect=KeyError("boom")), \
                contextlib.redirect_stdout(buf), self.assertRaises(KeyError):
            promote.rehearse(args, self.root)
        copy = next(l.split("=", 1)[1] for l in buf.getvalue().splitlines() if l.startswith("REHEARSAL_COPY="))
        self.assertFalse(os.path.lexists(Path(copy).parent))
        self.assertIn(f"REHEARSAL_TEMP_CLEANUP=PASS {Path(copy).parent}", buf.getvalue())

    def test_cleanup_failure_fails_the_rehearsal(self):
        with mock.patch.object(promote, "_remove_tree", return_value=False):
            rc, out, _ = self.run_rehearse(expect_removed=False)
        copy = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("REHEARSAL_COPY="))
        self.assertTrue(promote._remove_tree(Path(copy).parent))
        self.assertEqual(rc, 1)
        self.assertIn("REHEARSAL_TEMP_CLEANUP=FAIL", out)
        self.assertIn("MIGRATION_REHEARSAL=FAIL", out)

    def test_remove_tree_leaves_symlink_targets_alone(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.chmod(outside, 0o555)
        victim = self.tmp / "victim"
        (victim / "sealed").mkdir(parents=True)
        os.symlink(outside, victim / "sealed" / "link")
        os.chmod(victim / "sealed", 0o555)
        self.assertTrue(promote._remove_tree(victim))
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertEqual(os.stat(outside).st_mode & 0o777, 0o555, "a symlinked directory is never chmod'ed")


if __name__ == "__main__":
    unittest.main()
