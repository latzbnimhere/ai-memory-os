"""Regression tests for the production-hardening round (integrity, locking, path safety, recovery).

Runs in-process against the throwaway root provided by tests/_isolation.py; CLI
behaviour is exercised in real subprocesses bound to the same isolated root.
"""
from __future__ import annotations

import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from pathlib import Path

from aimem import cli, core, sessions, txn  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
AIMEM = REPO / "bin" / "aimem"
DETECT = REPO / "bin" / "aimem-detect"


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, text=True).stdout.strip()


def run(*args, bin_=AIMEM, check=False):
    env = dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT), PYTHONPATH=str(REPO / "lib"))
    r = subprocess.run([sys.executable, str(bin_), *map(str, args)], capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def kv(out, key):
    for line in out.splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1]
    return None


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(core.ROOT)
        assert _isolation.SANDBOX in root.parents, f"refusing to run outside the test sandbox: {root}"
        shutil.rmtree(root, ignore_errors=True)
        cli.main(["init", str(root)])
        cls.repo = _isolation.SANDBOX / f"repo-{cls.__name__}"
        shutil.rmtree(cls.repo, ignore_errors=True)
        cls.repo.mkdir(parents=True)
        git(cls.repo, "init", "-q")
        git(cls.repo, "config", "user.email", "test@example.invalid")
        git(cls.repo, "config", "user.name", "t")
        (cls.repo / "a.txt").write_text("a")
        git(cls.repo, "add", ".")
        git(cls.repo, "commit", "-qm", "one")
        cli.main(["register", "hard", "--name", "Hard", "--repo", str(cls.repo)])
        cls.p = core.project_dir("hard")

    def begin(self, task="t"):
        r = run("begin", "hard", "--agent", "other", "--task", task, check=True)
        return kv(r.stdout, "SESSION_ID")

    def close_all(self):
        for _f, j in sessions.open_sessions("hard"):
            sessions.close_session("hard", j["id"], "ABANDONED", "test cleanup")


class TestJournalIntegrity(Base):
    def test_append_after_torn_final_line_keeps_new_record_intact(self):
        f = self.p / "torn.jsonl"
        core.append_jsonl(f, {"n": 1})
        with f.open("a") as fh:
            fh.write('{"n": 2, "trunc')  # simulated crash mid-write: no trailing newline
        core.append_jsonl(f, {"n": 3})
        recs = core.read_jsonl(f)
        self.assertEqual([r["n"] for r in recs], [1, 3])
        lines = f.read_text().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(lines[1].startswith('{"n": 2'))

    def test_tail_lines_matches_full_read_on_large_unicode_file(self):
        f = self.p / "big.txt"
        body = [f"línea {i} — ✓ {'x' * (i % 97)}" for i in range(20000)]
        f.write_text("\n".join(body) + "\n", encoding="utf-8")
        self.assertEqual(core.tail_lines(f, 35), body[-35:])
        self.assertEqual(core.tail_lines(f, 1), body[-1:])
        self.assertEqual(core.tail_lines(self.p / "missing.txt", 5), [])
        self.assertEqual(core.tail_lines(f, 0), [])
        recs = self.p / "recs.jsonl"
        for i in range(50):
            core.append_jsonl(recs, {"i": i})
        with recs.open("a") as fh:
            fh.write("not json\n")
        self.assertEqual([r["i"] for r in core.tail_jsonl(recs, 4)], [47, 48, 49])


class TestPathSafety(Base):
    def test_unsafe_slugs_and_session_ids_rejected(self):
        for bad in ("../x", "..", ".hidden", "a/b", "a\\b", "", "x\x00y", "tab\tname"):
            with self.assertRaises(SystemExit, msg=repr(bad)):
                core.project_dir(bad)
        for bad in ("../../registry/projects", "a/b", ".x"):
            with self.assertRaises(SystemExit):
                sessions.session_file("hard", bad)
        self.assertEqual(core.project_dir("ok-slug_1").name, "ok-slug_1")

    def test_cli_refuses_traversal_and_unknown_projects_without_side_effects(self):
        before = sorted(str(x) for x in Path(core.ROOT).rglob("*"))
        r = run("note", "../../escape", "--kind", "info", "--text", "x")
        self.assertEqual(r.returncode, 2, r.stderr)
        r = run("artifact", "nosuch", "--path", str(self.repo / "a.txt"))
        self.assertEqual(r.returncode, 2)
        self.assertIn("Unknown project", r.stderr)
        r = run("session", "close", "hard", "--session", "../../../registry/projects")
        self.assertEqual(r.returncode, 2)
        after = sorted(str(x) for x in Path(core.ROOT).rglob("*") if ".locks" not in x.parts)
        self.assertEqual([x for x in after if x not in before], [])
        self.assertFalse((Path(core.ROOT).parent / "escape").exists())
        self.assertFalse((Path(core.ROOT) / "projects" / "nosuch").exists())

    def test_ambiguous_detection_fails_closed(self):
        cli.main(["register", "hard-dup", "--name", "Dup", "--repo", str(self.repo)])
        try:
            self.assertIsNone(core.detect_project(str(self.repo)))
            r = run(str(self.repo), bin_=DETECT)
            self.assertEqual(r.returncode, core.EXIT_AMBIGUOUS)
            self.assertIn("AMBIGUOUS_PROJECT", r.stderr)
        finally:
            reg = core.registry()
            reg["projects"].pop("hard-dup", None)
            core.save_registry(reg)
            shutil.rmtree(core.project_dir("hard-dup"), ignore_errors=True)
        self.assertEqual(core.detect_project(str(self.repo / "sub")), "hard")


class TestCheckpointsAndTransactions(Base):
    def test_cas_conflict_leaves_no_orphan_checkpoint(self):
        cps = self.p / "checkpoints"
        before = sorted(x.name for x in cps.iterdir())
        v = core.project_manifest("hard")["memory_version"]
        with self.assertRaises(SystemExit) as cm:
            sessions.create_checkpoint("hard", "conflict", "PASS", expect_version=v + 5)
        self.assertEqual(cm.exception.code, core.EXIT_CONFLICT)
        self.assertEqual(sorted(x.name for x in cps.iterdir()), before)

    def test_checkpoint_meta_is_committed_with_manifest(self):
        cp, version = sessions.create_checkpoint("hard", "sealed", "PASS")
        man = core.project_manifest("hard")
        meta = json.loads((self.p / "checkpoints" / cp / "meta.json").read_text())
        self.assertEqual(man["current_checkpoint"], cp)
        self.assertEqual(meta["memory_version"], version)
        self.assertEqual(meta["memory_version"], man["memory_version"])
        self.assertTrue(meta["txn"])
        self.assertEqual(meta["current_sha256"], core.sha256_file(self.p / "checkpoints" / cp / "CURRENT.md"))
        self.assertFalse(list((self.p / "checkpoints" / cp).glob(".*.staged")))

    def test_pending_transaction_blocks_new_canonical_writes(self):
        t = txn.Transaction("hard")
        t.stage(self.p / "NEXT.md", "INTERRUPTED\n")
        t._record("PREPARED")
        t._record("COMMITTING")
        try:
            with self.assertRaises(SystemExit) as cm:
                txn.canonical_update("hard", {self.p / "NEXT.md": "newer\n"})
            self.assertEqual(cm.exception.code, core.EXIT_RECOVERY_REQUIRED)
            cps = sorted(x.name for x in (self.p / "checkpoints").iterdir())
            with self.assertRaises(SystemExit) as cm:
                sessions.create_checkpoint("hard", "blocked", "PASS")
            self.assertEqual(cm.exception.code, core.EXIT_RECOVERY_REQUIRED)
            self.assertEqual(sorted(x.name for x in (self.p / "checkpoints").iterdir()), cps)
        finally:
            self.assertEqual(txn.repair("hard")[0][1], "ROLLED_FORWARD")
        self.assertEqual((self.p / "NEXT.md").read_text(), "INTERRUPTED\n")
        txn.canonical_update("hard", {self.p / "NEXT.md": "after repair\n"})

    def test_repair_never_rolls_stale_content_over_newer_edit(self):
        t = txn.Transaction("hard")
        t.stage(self.p / "CURRENT.md", "STALE STAGED\n")
        t._record("PREPARED")
        t._record("COMMITTING")
        (self.p / "CURRENT.md").write_text("NEWER HUMAN EDIT\n")  # someone edited after the crash
        self.assertEqual(txn.inspect("hard")[0]["resolution"], "MANUAL_TARGET_CHANGED")
        self.assertEqual(txn.repair("hard")[0][1], "TARGET_CHANGED_SINCE_PREPARE_MANUAL_REVIEW")
        self.assertEqual((self.p / "CURRENT.md").read_text(), "NEWER HUMAN EDIT\n")
        t.abort()  # manual resolution: discard the stale transaction
        self.assertEqual(txn.pending("hard"), [])

    def test_failure_mid_commit_keeps_recovery_record(self):
        t_before = len(txn.pending("hard"))
        orig = os.replace
        calls = {"n": 0}

        def flaky(src, dst):
            if str(src).endswith(".staged"):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("simulated disk failure mid-commit")
            return orig(src, dst)

        txn.os.replace = flaky
        try:
            with self.assertRaises(OSError):
                txn.canonical_update("hard", {self.p / "NEXT.md": "A\n", self.p / "CURRENT.md": "B\n"})
        finally:
            txn.os.replace = orig
        pend = txn.inspect("hard")
        self.assertEqual(len(pend), t_before + 1)
        self.assertEqual(pend[-1]["state"], "COMMITTING")
        self.assertEqual(txn.repair("hard")[-1][1], "ROLLED_FORWARD")
        self.assertEqual((self.p / "CURRENT.md").read_text(), "B\n")
        self.assertEqual((self.p / "NEXT.md").read_text(), "A\n")


class TestSessionConcurrency(Base):
    def test_heartbeats_racing_finish_never_reopen_session(self):
        self.close_all()
        sid = self.begin("race")
        (self.p / "CURRENT.md").write_text((self.p / "CURRENT.md").read_text() + "\n- race change\n")
        stop = threading.Event()
        errors = []

        def hammer():
            while not stop.is_set():
                try:
                    sessions.touch_heartbeat("hard", sid, "hb")
                except SystemExit:
                    return  # closed (or lock timeout): expected once finish wins
                except Exception as e:  # noqa: BLE001
                    errors.append(e)
                    return

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for th in threads:
            th.start()
        try:
            r = run("finish", "hard", "--session", sid, "--result", "PASS", "--label", "race")
        finally:
            stop.set()
            for th in threads:
                th.join(30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(errors, errors)
        st = json.loads(sessions.session_file("hard", sid).read_text())
        self.assertEqual(st["status"], "CLOSED")
        self.assertEqual(st["lease"]["state"], "CLOSED")
        r2 = run("finish", "hard", "--session", sid, "--result", "PASS", "--allow-unchanged")
        self.assertEqual(r2.returncode, 2)
        self.assertIn("not OPEN", r2.stderr)

    def test_finish_refuses_to_seal_secrets(self):
        self.close_all()
        sid = self.begin("secret")
        original = (self.p / "CURRENT.md").read_text()
        (self.p / "CURRENT.md").write_text(original + "\nkey " + "gh" + "p_" + "A" * 36 + "\n")
        cps = sorted(x.name for x in (self.p / "checkpoints").iterdir())
        r = run("finish", "hard", "--session", sid, "--result", "PASS")
        self.assertEqual(r.returncode, 2)
        self.assertIn("SECRET_PATTERN_REJECTED", r.stderr)
        self.assertEqual(sorted(x.name for x in (self.p / "checkpoints").iterdir()), cps)
        self.assertEqual(json.loads(sessions.session_file("hard", sid).read_text())["status"], "OPEN")
        (self.p / "CURRENT.md").write_text(original + "\n- cleaned\n")
        run("finish", "hard", "--session", sid, "--result", "PASS", check=True)

    def test_path_like_pwd_is_not_a_secret(self):
        self.assertFalse(core.secret_hits_text("pwd=/Users/someone/project"))
        self.assertTrue(core.secret_hits_text("password=hunter2hunter2"))

    def test_lock_timeout_reports_last_holder(self):
        with core.lock("holder-test", timeout=1):
            r = subprocess.run([sys.executable, "-c",
                                "from aimem import core\nwith core.lock('holder-test', timeout=0.3):\n    pass"],
                               capture_output=True, text=True,
                               env=dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT), PYTHONPATH=str(REPO / "lib")))
        self.assertEqual(r.returncode, core.EXIT_LOCK_TIMEOUT)
        self.assertIn(f'"pid": {os.getpid()}', r.stderr)


if __name__ == "__main__":
    unittest.main()
