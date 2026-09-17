"""Unit tests for AI Memory OS V4 internals (stdlib unittest; isolated temp root)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="aimem-unit-"))
ROOT = TMP / "memory"
os.environ["AI_MEMORY_ROOT"] = str(ROOT)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from aimem import chat, compact, core, index, objects, provenance, reconcile, sessions, txn  # noqa: E402
from aimem import cli  # noqa: E402


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, text=True).stdout.strip()


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Every TestCase subclass gets a pristine filesystem state.
        # core.ROOT remains the same path, but no registry/project state
        # may leak from the previous subclass.
        shutil.rmtree(TMP, ignore_errors=True)
        TMP.mkdir(parents=True, exist_ok=True)
        cli.main(["init", str(ROOT)])
        cls.repo = TMP / "repo"
        cls.repo.mkdir(exist_ok=True)
        git(cls.repo, "init", "-q")
        git(cls.repo, "config", "user.email", "test@example.invalid")
        git(cls.repo, "config", "user.name", "t")
        (cls.repo / "a.txt").write_text("a")
        git(cls.repo, "add", ".")
        git(cls.repo, "commit", "-qm", "one")
        cli.main(["register", "unit", "--name", "Unit", "--repo", str(cls.repo)])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)


class TestCore(Base):
    def test_atomic_write_validate_rejects(self):
        p = ROOT / "x.json"
        with self.assertRaises(Exception):
            core.atomic_write(p, "{not json", validate=core.validate_json_file)
        self.assertFalse(p.exists())
        self.assertFalse(list(ROOT.glob(".x.json.*")), "temp file must be cleaned")
        core.atomic_write(p, '{"ok": 1}', validate=core.validate_json_file)
        self.assertEqual(json.loads(p.read_text())["ok"], 1)

    def test_redact_and_secret_hits(self):
        s = core.redact("password"+"=abc123 token: xyz "+"sk"+"-"+"ABCDEFGHIJKLMNOPQRSTUVWXYZ1234")
        self.assertNotIn("abc123", s)
        self.assertNotIn("sk-ABCDEF", s)
        self.assertIn("[REDACTED_SECRET]", s)
        self.assertTrue(core.secret_hits_text("-----BEGIN "+"RSA PRIVATE KEY-----"))
        self.assertTrue(core.secret_hits_text("AKIA" + "ABCDEFGHIJKLMNOP"))
        self.assertFalse(core.secret_hits_text("nothing secret here"))

    def test_recursive_index_risk(self):
        self.assertTrue(core.recursive_index_risk(str(ROOT)))
        self.assertTrue(core.recursive_index_risk(str(ROOT / "projects")))
        self.assertTrue(core.recursive_index_risk(str(ROOT.parent)))
        self.assertFalse(core.recursive_index_risk(str(self.repo)))

    def test_detect(self):
        self.assertEqual(core.detect_project(str(self.repo / "deep" / "er")), "unit")
        self.assertIsNone(core.detect_project(str(TMP)))

    def test_lock_timeout(self):
        with core.lock("unit-test", timeout=1):
            r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0,{str(Path(__file__).resolve().parents[1] / 'lib')!r}); from aimem import core\nwith core.lock('unit-test', timeout=0.3): pass"],
                               env=dict(os.environ, AI_MEMORY_ROOT=str(ROOT)), capture_output=True, text=True)
            self.assertEqual(r.returncode, core.EXIT_LOCK_TIMEOUT)
            self.assertIn("LOCK_TIMEOUT", r.stderr)


class TestTxn(Base):
    def test_commit_and_journal(self):
        p = core.project_dir("unit")
        t = txn.Transaction("unit", kind="unit")
        t.stage(p / "NEXT.md", "# next\n")
        t.stage_json(p / "extra.json", {"a": 1})
        tid = t.commit()
        self.assertTrue(tid)
        self.assertEqual((p / "NEXT.md").read_text(), "# next\n")
        self.assertFalse(list(p.glob("*.staged")))
        self.assertFalse(txn.pending("unit"))
        j = core.read_jsonl(p / "cold" / "txn-journal" / f"{core.day()}.jsonl")
        self.assertTrue(any(x["txn"] == tid for x in j))

    def test_abort_cleans(self):
        p = core.project_dir("unit")
        t = txn.Transaction("unit")
        t.stage(p / "NEXT.md", "SHOULD_NOT")
        t.abort()
        self.assertNotIn("SHOULD_NOT", (p / "NEXT.md").read_text())
        self.assertFalse(list(p.glob(".NEXT.md.*.staged")))

    def test_committing_roll_forward_and_prepared_rollback(self):
        p = core.project_dir("unit")
        t = txn.Transaction("unit")
        t.stage(p / "NEXT.md", "FORWARD\n")
        t._record("PREPARED")
        t._record("COMMITTING")
        rep = txn.inspect("unit")
        self.assertEqual(rep[0]["resolution"], "ROLL_FORWARD")
        self.assertEqual(txn.repair("unit")[0][1], "ROLLED_FORWARD")
        self.assertEqual((p / "NEXT.md").read_text(), "FORWARD\n")
        t2 = txn.Transaction("unit")
        t2.stage(p / "NEXT.md", "NEVER\n")
        t2._record("PREPARED")
        os.unlink(t2.entries[0]["staged"])
        self.assertEqual(txn.inspect("unit")[0]["resolution"], "ROLL_BACK")
        self.assertEqual(txn.repair("unit")[0][1], "ROLLED_BACK")
        self.assertEqual((p / "NEXT.md").read_text(), "FORWARD\n")

    def test_canonical_update_cas(self):
        p = core.project_dir("unit")
        v0 = core.project_manifest("unit").get("memory_version", 0)
        v1, _ = txn.canonical_update("unit", {p / "NEXT.md": "v1\n"}, expect_version=v0)
        self.assertEqual(v1, v0 + 1)
        with self.assertRaises(SystemExit) as cm:
            txn.canonical_update("unit", {p / "NEXT.md": "v2\n"}, expect_version=v0)
        self.assertEqual(cm.exception.code, core.EXIT_CONFLICT)
        self.assertEqual((p / "NEXT.md").read_text(), "v1\n")
        self.assertFalse(list(p.glob(".*.staged")))


class TestSessions(Base):
    def test_lease_states(self):
        cfg = {"heartbeat_stale_s": 100, "heartbeat_abandoned_s": 200}
        now = core.now()
        import datetime as dt
        j = {"status": "OPEN", "lease": {"last_heartbeat": (now - dt.timedelta(seconds=10)).isoformat()}}
        self.assertEqual(sessions.lease_state(j, cfg), "ACTIVE")
        j["lease"]["last_heartbeat"] = (now - dt.timedelta(seconds=150)).isoformat()
        self.assertEqual(sessions.lease_state(j, cfg), "STALE")
        j["lease"]["last_heartbeat"] = (now - dt.timedelta(seconds=250)).isoformat()
        self.assertEqual(sessions.lease_state(j, cfg), "ABANDONED")
        j["status"] = "CLOSED"
        self.assertEqual(sessions.lease_state(j, cfg), "CLOSED")
        self.assertEqual(sessions.lease_state({"status": "OPEN"}, cfg), "STALE", "no heartbeat and no start -> STALE, never ACTIVE")

    def test_resolve_fail_closed(self):
        s1, _ = sessions.begin("unit", "claude", "t1")
        s2, _ = sessions.begin("unit", "codex", "t2")
        with self.assertRaises(SystemExit) as cm:
            sessions.resolve_session("unit", None, env_ok=False)
        self.assertEqual(cm.exception.code, core.EXIT_AMBIGUOUS)
        self.assertEqual(sessions.resolve_session("unit", s1["id"])[0], s1["id"])
        with self.assertRaises(SystemExit):
            sessions.resolve_session("unit", "missing")
        sessions.close_session("unit", s1["id"])
        sessions.close_session("unit", s2["id"])
        with self.assertRaises(SystemExit):
            sessions.resolve_session("unit", s1["id"])  # closed


class TestReconcileProvenance(Base):
    def test_reconcile_classes(self):
        cli.main(["capture", "unit"])
        r = reconcile.reconcile("unit")
        self.assertEqual(r["status"], "MEMORY_MATCH")
        (self.repo / "b.txt").write_text("b")
        r = reconcile.reconcile("unit")
        self.assertIn("DIRTY_REPO", r["flags"])
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "two")
        r = reconcile.reconcile("unit")
        self.assertEqual(r["status"], "PHYSICAL_AHEAD")
        self.assertEqual(r["commits_ahead"], 1)
        self.assertIn("b.txt", r["changed_files"])
        cli.main(["capture", "unit"])
        git(self.repo, "checkout", "-q", "HEAD~1")
        r = reconcile.reconcile("unit")
        self.assertEqual(r["status"], "MEMORY_AHEAD_OR_UNVERIFIED")
        git(self.repo, "checkout", "-q", "-")
        git(self.repo, "checkout", "-q", "-b", "side", "HEAD~1")
        (self.repo / "c.txt").write_text("c")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "side")
        r = reconcile.reconcile("unit")
        self.assertEqual(r["status"], "DIVERGED")
        git(self.repo, "checkout", "-q", "master")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "-q", "main"], capture_output=True)
        self.assertIn(reconcile.reconcile("unit")["status"], ("MEMORY_MATCH", "DIVERGED", "PHYSICAL_AHEAD", "MEMORY_AHEAD_OR_UNVERIFIED"))

    def test_provenance_stale_view(self):
        provenance.record_fact("unit", "repo.head", "0" * 40, "PHYSICAL_GIT", status="VERIFIED")
        live = core.repo_state_for("unit")
        view = provenance.latest_view("unit", live)
        self.assertEqual(view["repo.head"]["status"], "STALE")
        with self.assertRaises(SystemExit):
            provenance.record_fact("unit", "k", "v", "NOPE")
        with self.assertRaises(SystemExit):
            provenance.record_fact("unit", "k", "v", "DERIVED", status="MAYBE")


class TestIndexCompactObjects(Base):
    def test_chunks_and_ranking(self):
        text = "\n".join(["# H"] + ["line %d" % i for i in range(2000)])
        chunks = index.split_chunks(text, max_chars=3500)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 3500 for c in chunks))
        p = core.project_dir("unit")
        (p / "CURRENT.md").write_text("# Unit\nSTATUS: needle_alpha found here\n")
        (p / "knowledge").mkdir(exist_ok=True)
        (p / "knowledge" / "old.md").write_text("needle_alpha in an old knowledge note\n" * 3)
        index.reindex("unit", quiet=True)
        rows = index.fts_search("unit", "needle_alpha", 5)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["kind"], "hot_current", rows)
        self.assertEqual(index.fts_search("unit", "", 5), [])

    def test_compaction_deterministic(self):
        p = core.project_dir("unit")
        j = p / "cold" / "step-journal" / "2026-01-01.jsonl"
        j.parent.mkdir(parents=True, exist_ok=True)
        for k in range(5):
            core.append_jsonl(j, {"time": f"2026-01-01T00:00:0{k}+00:00", "step_kind": "write" if k else "error", "session_id": "s1", "agent": "claude",
                                  "summary": f"step {k}", "result": "PASS", "files": ["f.py"], "repo_state": {"head": "abc", "branch": "main"}})
        r1 = compact.compact_project("unit")
        self.assertGreaterEqual(r1["daily_written"], 1)
        md = (p / "cold" / "summaries" / "daily" / "2026-01-01.md").read_text()
        self.assertIn("step 0", md)
        r2 = compact.compact_project("unit")
        self.assertEqual(r2["daily_written"], 0)
        r3 = compact.compact_project("unit", rebuild=True)
        md2 = (p / "cold" / "summaries" / "daily" / "2026-01-01.md").read_text()
        strip = lambda t: [l for l in t.splitlines() if not l.startswith("GENERATED:")]
        self.assertEqual(strip(md), strip(md2))
        self.assertTrue(j.exists(), "raw must remain")
        dr = compact.compact_project("unit", dry_run=True, rebuild=True)
        self.assertGreaterEqual(dr["daily_written"], 1)

    def test_objects_dedup_verify(self):
        f = TMP / "obj.bin"
        f.write_bytes(b"Z" * 5000)
        sha, path, dedup = objects.store_file(f)
        self.assertFalse(dedup)
        sha2, path2, dedup2 = objects.store_file(f)
        self.assertTrue(dedup2 and sha == sha2 and path == path2)
        self.assertEqual(path.parent.name, sha[2:4])
        ok, bad, mis = objects.verify_all()
        self.assertEqual(bad, [])
        self.assertGreaterEqual(ok, 1)
        s, tp, d = objects.store_text("hello")
        self.assertEqual(tp.read_text(), "hello")


class TestChat(Base):
    def test_parse_packet(self):
        pk = chat.parse_packet("""================ AI_MEMORY_AGENT_PACKET BEGIN ================
PACKET_VERSION:
AI_MEMORY_AGENT_PACKET_V1
SOURCE: CHATGPT_EXISTING_CHAT
PROJECT_SLUG:
unit
CURRENT_AUTHORITATIVE_STATE
* Branch: `main`
* HEAD: `abcdefabcdefabcdefabcdefabcdefabcdefabcd`
EXACT_NEXT_ACTION
do x
================ AI_MEMORY_AGENT_PACKET END =================""")
        self.assertEqual(pk["fields"]["PACKET_VERSION"], "AI_MEMORY_AGENT_PACKET_V1")
        self.assertEqual(pk["fields"]["SOURCE"], "CHATGPT_EXISTING_CHAT")
        self.assertEqual(pk["fields"]["PROJECT_SLUG"], "unit")
        self.assertEqual(pk["heads"], ["abcdefabcdefabcdefabcdefabcdefabcdefabcd"])
        self.assertIn("main", pk["branches"])
        self.assertIn("EXACT_NEXT_ACTION", pk["sections"])
        self.assertEqual(pk["errors"], [])
        bad = chat.parse_packet("PACKET_VERSION: V0\n")
        self.assertTrue(bad["errors"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


# V4_DISCOVERY_ISOLATION_R1
#
# unittest discovery imports multiple test modules into one interpreter.
# AI Memory intentionally binds AI_MEMORY_ROOT at import time, so V4 test
# classes must execute in fresh processes to prevent another test module's
# root from contaminating this module.
class TestDiscoveryIsolation(unittest.TestCase):

    def test_v4_classes_in_fresh_processes(self):
        test_dir = Path(__file__).resolve().parent
        repo = test_dir.parent

        class_names = [
            name
            for name, obj in globals().items()
            if (
                isinstance(obj, type)
                and obj is not Base
                and issubclass(obj, Base)
                and obj.__module__ == __name__
            )
        ]

        self.assertTrue(class_names, "no V4 Base subclasses discovered")

        failures = []

        for name in class_names:
            print()
            print("=" * 72)
            print(f"ISOLATED_V4_CLASS={name}")
            print("=" * 72)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(repo / "lib")

            r = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "-v",
                    f"test_v4.{name}",
                ],
                cwd=str(test_dir),
                env=env,
                capture_output=True,
                text=True,
            )

            if r.stdout:
                print(r.stdout)

            if r.stderr:
                print(r.stderr, file=sys.stderr)

            if r.returncode != 0:
                failures.append(
                    f"{name}: rc={r.returncode}\n"
                    f"STDOUT:\n{r.stdout}\n"
                    f"STDERR:\n{r.stderr}"
                )

        self.assertFalse(
            failures,
            "\n\n".join(failures),
        )

        print("V4_DISCOVERY_ISOLATED_CLASSES=PASS")


def load_tests(loader, standard_tests, pattern):
    # Full discovery gets exactly one wrapper test. Direct targets such as
    # `python -m unittest test_v4.TestCore` still execute that class normally.
    return unittest.TestSuite([
        TestDiscoveryIsolation(
            "test_v4_classes_in_fresh_processes"
        )
    ])
