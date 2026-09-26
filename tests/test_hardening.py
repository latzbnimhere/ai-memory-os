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
        self.assertFalse(core.secret_hits_text("pwd=/srv/work/project"))
        self.assertTrue(core.secret_hits_text("password=hunter2hunter2"))

    def test_lock_timeout_reports_last_holder(self):
        with core.lock("holder-test", timeout=1):
            r = subprocess.run([sys.executable, "-c",
                                "from aimem import core\nwith core.lock('holder-test', timeout=0.3):\n    pass"],
                               capture_output=True, text=True,
                               env=dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT), PYTHONPATH=str(REPO / "lib")))
        self.assertEqual(r.returncode, core.EXIT_LOCK_TIMEOUT)
        self.assertIn(f'"pid": {os.getpid()}', r.stderr)


class TestImportBackupMigrate(Base):
    def test_chat_import_never_persists_secret_packets(self):
        pkt = _isolation.SANDBOX / "packet.txt"
        pkt.write_text("AI_MEMORY_AGENT_PACKET BEGIN\nPACKET_VERSION: AI_MEMORY_AGENT_PACKET_V1\nPROJECT_SLUG: hard\n"
                       "NOTE: leaked " + "sk" + "-" + "Z" * 40 + "\nAI_MEMORY_AGENT_PACKET END\n")
        r = run("chat-import", "hard", str(pkt))
        self.assertEqual(r.returncode, core.EXIT_CONFLICT)
        self.assertIn("REFUSED_SECRET_PATTERN", r.stdout)
        kdir = self.p / "knowledge" / "chat-imports"
        self.assertFalse(kdir.exists() and any(kdir.iterdir()))
        leaked = [f for f in Path(core.ROOT).rglob("*") if f.is_file() and ".generated" not in f.parts
                  and "Z" * 40 in f.read_text(errors="ignore")]
        self.assertEqual(leaked, [])

    def _tar(self, name, build):
        import tarfile
        t = _isolation.SANDBOX / name
        with tarfile.open(t, "w:gz") as tf:
            build(tf)
        return t

    def test_backup_refuses_escaping_links_but_allows_contained_ones(self):
        import io
        import tarfile
        from aimem import backup

        def add_file(tf, name, data=b"x"):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))

        def add_link(tf, name, target, kind=tarfile.SYMTYPE):
            ti = tarfile.TarInfo(name)
            ti.type = kind
            ti.linkname = target
            tf.addfile(ti)

        evil = self._tar("evil.tar.gz", lambda tf: (add_link(tf, "AI-Memory/escape", "../../outside"),
                                                     add_file(tf, "AI-Memory/escape/pwned")))
        evil2 = self._tar("evil2.tar.gz", lambda tf: add_link(tf, "AI-Memory/h", "/etc/passwd", tarfile.LNKTYPE))
        good = self._tar("good.tar.gz", lambda tf: (add_file(tf, "AI-Memory/registry/projects.json", b"{}"),
                                                     add_link(tf, "AI-Memory/alias", "registry/projects.json")))
        for bad in (evil, evil2):
            with tarfile.open(bad) as tf:
                self.assertTrue(backup.unsafe_members(tf))
            rep = backup.verify(bad)
            self.assertEqual(rep["result"], "FAIL")
            self.assertIn(("tar_paths_safe", "FAIL"), rep["checks"])
        with tarfile.open(good) as tf:
            self.assertEqual(backup.unsafe_members(tf), [])
        self.assertFalse((_isolation.SANDBOX / "outside").exists())

    def test_migrate_refuses_newer_data_without_mutation(self):
        from aimem import migrate
        root = Path(core.ROOT)
        snap = lambda: {str(f): f.read_bytes() for f in root.rglob("*") if f.is_file() and ".locks" not in f.parts}
        vfile = root / "VERSION"
        orig = vfile.read_text()
        try:
            vfile.write_text("9.0.0\n")
            before = snap()
            with self.assertRaises(SystemExit):
                migrate.plan_and_apply(dry_run=True)
            with self.assertRaises(SystemExit):
                migrate.plan_and_apply()
            self.assertEqual(snap(), before)
        finally:
            vfile.write_text(orig)
        man = core.project_manifest("hard")
        try:
            man2 = dict(man, engine_version="7.0.0")
            core.atomic_write_json(self.p / "project.json", man2)
            with self.assertRaises(SystemExit):
                migrate.plan_and_apply()
            self.assertEqual(core.project_manifest("hard")["engine_version"], "7.0.0")
        finally:
            core.atomic_write_json(self.p / "project.json", man)
        cfg = core.try_load_json(core.CONFIG, {})
        try:
            core.atomic_write_json(core.CONFIG, dict(cfg, version=5))
            with self.assertRaises(SystemExit):
                migrate.plan_and_apply()
            self.assertEqual(core.try_load_json(core.CONFIG, {})["version"], 5)
        finally:
            core.atomic_write_json(core.CONFIG, cfg)
        migrate.plan_and_apply()
        self.assertNotIn("migrated_from", core.project_manifest("hard"), "native V4 project must not claim V3 origin")
        plan = migrate.plan_and_apply(dry_run=True)  # idempotent: nothing left to change in manifests
        self.assertFalse([a for a in plan if "project.json" in a], plan)


class TestContextAndRetrieval(Base):
    def setUp(self):
        self.close_all()
        self.cur = self.p / "CURRENT.md"
        self.nxt = self.p / "NEXT.md"
        self.cur_orig, self.nxt_orig = self.cur.read_text(), self.nxt.read_text()

    def tearDown(self):
        self.cur.write_text(self.cur_orig)
        self.nxt.write_text(self.nxt_orig)

    def build(self, query="", tokens=4500, mode="smart"):
        from aimem import context
        return context.build_context("hard", query, tokens, mode)

    def test_next_survives_oversized_current_in_every_mode(self):
        self.cur.write_text("# CUR\n" + "authoritative-current-state-line\n" * 4000)
        self.nxt.write_text("# NEXT\n- EXACT_CONTINUATION_MARKER run the dry-run\n")
        for mode, tokens in (("hot", 4500), ("smart", 4500), ("deep", 6000), ("smart", 1200)):
            ctx = self.build("", tokens, mode)
            budget = int(ctx.split("APPROX_TOKEN_BUDGET: ")[1].split()[0])
            self.assertIn("EXACT_CONTINUATION_MARKER", ctx, mode)
            self.assertIn("## CURRENT AUTHORITATIVE STATE", ctx, mode)
            self.assertLessEqual(core.approx_tokens(ctx), budget, mode)

    def test_journals_rendered_compactly_and_newest_kept(self):
        sid = self.begin("journal")
        for i in range(60):
            run("note", "hard", "--kind", "info", "--text", f"event-number-{i:03d} " + "pad " * 40, "--session", sid, check=True)
        ctx = self.build("", 1500, "hot")
        self.assertIn("event-number-059", ctx)          # newest survives truncation
        self.assertNotIn("event-number-000", ctx)       # oldest is what gets dropped
        self.assertNotIn('"status_lines"', ctx)          # no raw repo-state JSON dumps
        self.assertNotIn('"log_policy"', ctx)

    def test_context_redacts_credentials_found_in_canonical_files(self):
        token = "gh" + "p_" + "B" * 36
        self.cur.write_text(self.cur_orig + f"\nleaked {token}\n")
        ctx = self.build("")
        self.assertNotIn(token, ctx)
        self.assertIn("[REDACTED_SECRET]", ctx)

    def test_context_is_deterministic_for_same_inputs(self):
        # only wall-clock stamps may differ between two builds over unchanged inputs
        strip = lambda c: "\n".join(l for l in c.splitlines() if not l.startswith("GENERATED: ") and '"captured_at"' not in l)
        a, b = self.build("hard state"), self.build("hard state")
        self.assertGreater(len(a.splitlines()), 40)
        self.assertEqual(strip(a), strip(b))

    def test_incremental_reindex_and_auto_refreshing_search(self):
        from aimem import index
        index.reindex("hard", quiet=True, full=True)
        con = index.db_connect()
        try:
            self.assertEqual(index._refresh(con, "hard")[2:], (0, 0))
            f = self.p / "knowledge" / "note.md"
            f.parent.mkdir(exist_ok=True)
            f.write_text("# note\nzebracornflake appears here\n")
            self.assertEqual(index._refresh(con, "hard")[2], 1)
            os.utime(f, None)  # touched but unchanged content: no re-chunking
            self.assertEqual(index._refresh(con, "hard")[2], 0)
            f.unlink()
            self.assertEqual(index._refresh(con, "hard")[3], 1)
        finally:
            con.close()
        self.assertEqual(index.fts_search("hard", "zebracornflake"), [])
        run("note", "hard", "--kind", "decision", "--text", "adopt quokkaprotocol for sync", check=True)
        hits = index.fts_search("hard", "quokkaprotocol")  # no explicit reindex in between
        self.assertTrue(hits and hits[0]["kind"] == "decision", hits)
        a = [(h["path"], h["chunk_no"]) for h in index.fts_search("hard", "hard project state")]
        b = [(h["path"], h["chunk_no"]) for h in index.fts_search("hard", "hard project state")]
        self.assertEqual(a, b)

    def test_corrupt_index_is_quarantined_and_rebuilt_on_search(self):
        from aimem import index
        run("note", "hard", "--kind", "decision", "--text", "choose wombatstrategy", check=True)
        index.reindex(None, quiet=True)
        for suf in ("-wal", "-shm"):
            Path(str(core.DB) + suf).unlink(missing_ok=True)
        core.DB.write_bytes(b"this is not a sqlite database" * 100)
        hits = index.fts_search("hard", "wombatstrategy")
        self.assertTrue(hits)
        self.assertTrue(list(core.DB.parent.glob("memory.db.corrupt-*")))
        self.assertEqual(index.db_health(), "OK")

    def test_legacy_index_schema_is_rebuilt(self):
        import sqlite3
        from aimem import index
        with core.lock("index"):
            for suf in ("", "-wal", "-shm"):
                Path(str(core.DB) + suf).unlink(missing_ok=True)
            con = sqlite3.connect(str(core.DB))
            con.execute("CREATE TABLE chunks_meta(id INTEGER PRIMARY KEY, project TEXT, path TEXT, kind TEXT, chunk_no INT, sha256 TEXT)")
            con.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(body, project UNINDEXED, path UNINDEXED, kind UNINDEXED, chunk_no UNINDEXED)")
            con.commit()
            con.close()
        files, chunks = index.reindex(None, quiet=True)
        self.assertGreater(chunks, 0)
        con = index.db_connect()
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], index.SCHEMA_VERSION)
        con.close()


class TestDoctor(Base):
    def doctor(self, *extra):
        return run("doctor", "--deep", "--no-repo", "--slug", "hard", *extra)

    def test_torn_journal_line_detected_and_quarantined_losslessly(self):
        ev = self.p / "EVENTS.jsonl"
        core.append_jsonl(ev, {"kind": "before", "n": 1})
        with ev.open("a") as fh:
            fh.write('{"kind": "torn", "n": 2, "x')  # crash mid-append
        core.append_jsonl(ev, {"kind": "after", "n": 3})
        original = ev.read_bytes()
        r = self.doctor()
        self.assertEqual(r.returncode, 1)
        self.assertIn("invalid JSONL EVENTS.jsonl", r.stdout)
        r = self.doctor("--repair")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("quarantined 1 invalid line(s) from EVENTS.jsonl", r.stdout)
        kinds = [x.get("kind") for x in core.read_jsonl(ev)]
        self.assertIn("before", kinds)
        self.assertIn("after", kinds)
        q = self.p / "cold" / "quarantine"
        self.assertEqual(next(q.glob("EVENTS.jsonl.*.orig")).read_bytes(), original)
        self.assertIn(b'"torn"', next(q.glob("EVENTS.jsonl.*.bad")).read_bytes())
        self.assertEqual(self.doctor().returncode, 0)

    def test_stale_temp_files_and_incomplete_checkpoints_reported(self):
        tmp = self.p / ".CURRENT.md.abc123.tmp"
        tmp.write_text("partial")
        old = __import__("time").time() - 7200
        os.utime(tmp, (old, old))
        inc = self.p / "checkpoints" / "20250101T000000+0000__interrupted"
        inc.mkdir(parents=True)
        (inc / "CURRENT.md").write_text("x")
        legacy = self.p / "checkpoints" / "20250101T000001+0000__refused"
        legacy.mkdir()
        (legacy / "CURRENT.md").write_text("y")
        core.atomic_write_json(legacy / "meta.json", {"id": legacy.name, "engine_version": "4.1.0", "result": "PASS"})
        try:
            r = self.doctor()
            self.assertIn("stale temp file", r.stdout)
            self.assertIn("incomplete checkpoint 20250101T000000+0000__interrupted", r.stdout)
            self.assertIn("20250101T000001+0000__refused was never committed", r.stdout)
            self.doctor("--repair")
            self.assertFalse(tmp.exists())
            self.assertTrue(inc.exists(), "doctor must never delete checkpoint data")
        finally:
            shutil.rmtree(inc, ignore_errors=True)
            shutil.rmtree(legacy, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
