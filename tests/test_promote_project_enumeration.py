"""Regression tests for rehearsal project enumeration (isolated temp roots; never touches ~/AI-Memory).

A real rehearsal failed with NotADirectoryError on projects/.DS_Store/project.json: the canonical snapshot
enumerated projects with Path.glob("*/"), which on Python < 3.11 also yields plain files.
"""
from __future__ import annotations

import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))

import promote  # noqa: E402


class TestRehearsalProjectEnumeration(unittest.TestCase):
    SLUGS = ("alpha", "beta")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="aimem-promote-enum-"))
        self.root = self.tmp / "AI-Memory"
        (self.root / "registry").mkdir(parents=True)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "aimem").write_text("#!/usr/bin/env python3\n")
        for slug in self.SLUGS:
            self.make_project(slug)
        self.write_registry(self.SLUGS)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

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
        shutil.rmtree(self.root / "projects" / "beta")
        (self.root / "projects" / "beta").write_text("oops")
        self.assert_fails_closed("beta: registered but projects/beta is not a project directory")

    def test_registered_slug_without_directory_fails_closed(self):
        self.write_registry(self.SLUGS + ("ghost",))
        self.assert_fails_closed("ghost: registered but projects/ghost is not a project directory")

    def test_invalid_registry_fails_closed(self):
        (self.root / "registry" / "projects.json").write_text("{not json")
        self.assert_fails_closed("registry/projects.json unreadable")

    # ------------------------------------------------------------ rehearse() end to end (promotion stubbed)
    def run_rehearse(self, promotion_rc=0):
        """rehearse() on self.root with the Promotion replaced by a stub (the real one runs this very test suite
        as a gate). Reports go to a temp DEV so the repository tree is not written."""
        dev = self.tmp / "dev"
        dev.mkdir(exist_ok=True)
        made = []

        class StubPromotion:
            def __init__(self, root, **kw):
                made.append(root)
                self.report = {"result": "PASS" if promotion_rc == 0 else "FAIL", "gates": {}}

            def run(self):
                return promotion_rc

        args = argparse.Namespace(skip_selftest=True, smoke_slug="alpha", keep=False)
        buf = io.StringIO()
        with mock.patch.object(promote, "Promotion", StubPromotion), mock.patch.object(promote, "DEV", dev), \
                contextlib.redirect_stdout(buf):
            rc = promote.rehearse(args, self.root)
        out = buf.getvalue()
        copy = next((l.split("=", 1)[1] for l in out.splitlines() if l.startswith("REHEARSAL_COPY=")), None)
        if copy:
            self.assertFalse(Path(copy).parent.exists(), "the rehearsal copy is always removed")
        return rc, out, made

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

    def test_rehearse_cleans_up_the_copy_when_it_raises(self):
        # the original failure raised out of rehearse() and left a full copy of the private root in $TMPDIR
        args = argparse.Namespace(skip_selftest=True, smoke_slug="alpha", keep=False)
        buf = io.StringIO()
        with mock.patch.object(promote, "_isolate_copy", side_effect=KeyError("boom")), \
                contextlib.redirect_stdout(buf), self.assertRaises(KeyError):
            promote.rehearse(args, self.root)
        copy = next(l.split("=", 1)[1] for l in buf.getvalue().splitlines() if l.startswith("REHEARSAL_COPY="))
        self.assertFalse(Path(copy).parent.exists())
        self.assertTrue((self.root / "projects" / "alpha" / "project.json").is_file())


if __name__ == "__main__":
    unittest.main()
