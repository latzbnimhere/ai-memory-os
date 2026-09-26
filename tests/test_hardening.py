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

    def test_index_bookkeeping_drift_forces_project_rebuild(self):
        from aimem import index
        run("note", "hard", "--kind", "decision", "--text", "pick platypusplan", check=True)
        index.reindex("hard", quiet=True)
        con = index.db_connect()
        try:  # what a pre-4.2 engine's reindex does after a rollback: rewrite chunks, ignore `files`
            con.execute("DELETE FROM chunks_meta WHERE project='hard'")
            con.execute("DELETE FROM chunks_fts WHERE project='hard'")
            con.commit()
        finally:
            con.close()
        self.assertTrue(index.fts_search("hard", "platypusplan"))

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


class TestReviewFindings(Base):
    """Regressions for issues found by adversarial review of this hardening round."""

    def test_unicode_line_separators_do_not_drop_records(self):
        f = self.p / "u2028.jsonl"
        core.append_jsonl(f, {"n": 1})
        core.append_jsonl(f, {"n": 2, "text": "pasted\u2028line\u2029and\u0085more"})
        core.append_jsonl(f, {"n": 3})
        self.assertEqual([r["n"] for r in core.tail_jsonl(f, 10)], [1, 2, 3])
        self.assertEqual(len(core.tail_lines(f, 10)), 3)

    def test_non_object_json_record_does_not_break_context(self):
        from aimem import context
        ev = self.p / "EVENTS.jsonl"
        with ev.open("a") as fh:
            fh.write('"hello"\n[1, 2]\n42\n')
        ctx = context.build_context("hard", "", 4500)
        self.assertIn("## NEXT ACTIONS", ctx)

    def test_diagnostics_reach_registered_project_with_missing_manifest(self):
        cli.main(["register", "broken", "--name", "Broken"])
        try:
            (core.project_dir("broken") / "project.json").unlink()
            r = run("doctor", "--slug", "broken", "--no-repo")
            self.assertIn("missing project.json", r.stdout)
            self.assertNotIn("Unknown project", r.stderr)
            r = run("txn", "broken")
            self.assertNotIn("Unknown project", r.stderr)
        finally:
            reg = core.registry()
            reg["projects"].pop("broken", None)
            core.save_registry(reg)
            shutil.rmtree(core.project_dir("broken"), ignore_errors=True)

    def test_allocate_never_exceeds_budget(self):
        import random
        from aimem import context
        rnd = random.Random(7)
        for _ in range(3000):
            secs = []
            for i in range(rnd.randint(1, 13)):
                body = "\n".join("x" * rnd.randint(0, 120) for _ in range(rnd.randint(1, 80))) + "y"
                secs.append({"title": f"S{i}", "body": body, "authority": "A", "priority": rnd.randint(1, 13),
                             "cap": rnd.choice([0.08, 0.1, 0.15, 0.25, 0.5]), "keep_tail": rnd.random() < 0.4})
            budget = rnd.randint(0, 20000)
            out = "".join(context.allocate(secs, budget))
            self.assertLessEqual(len(out), budget)

    def test_backup_with_external_symlink_and_hardlink_verifies(self):
        from aimem import backup
        ext = _isolation.SANDBOX / "outside-target.txt"
        ext.write_text("outside")
        link = self.p / "artifacts" / "external-link.txt"
        hard = self.p / "artifacts" / "hardlinked.txt"
        link.parent.mkdir(exist_ok=True)
        (self.p / "artifacts" / "orig.txt").write_text("same inode")
        try:
            os.symlink(ext, link)
            os.link(self.p / "artifacts" / "orig.txt", hard)
            out = _isolation.SANDBOX / "backups"
            tarball = backup.create(out, "linktest")
            meta = json.loads(backup.sidecar(tarball, "meta.json").read_text())
            self.assertIn("AI-Memory/projects/hard/artifacts/external-link.txt", meta["skipped_external_symlinks"])
            rep = backup.verify(tarball)
            self.assertEqual(rep["result"], "PASS", rep)
        finally:
            for f in (link, hard, self.p / "artifacts" / "orig.txt"):
                if f.is_symlink() or f.exists():
                    f.unlink()

    def test_session_record_without_id_does_not_break_recovery_scans(self):
        from aimem import recover
        sid = "20250101T000000+0000-other-noid00"
        core.atomic_write_json(sessions.session_file("hard", sid), {"status": "OPEN", "started_at": "2025-01-01T00:00:00+00:00"})
        try:
            ids = [a["id"] for a in recover.scan("hard")]
            self.assertIn(sid, ids)
            self.assertEqual(run("sessions", "hard").returncode, 0)
            self.assertEqual(core.version_tuple("V3.1.1"), (3, 1, 1))
        finally:
            sessions.session_file("hard", sid).unlink()


def _mid_ff(b):
    n = len(b) // 2
    return b[:n] + b"\xff" * 8192 + b[n + 8192:]


def _mid_zero(b):
    n = len(b) // 2
    return b[:n] + b"\x00" * 8192 + b[n + 8192:]


def _rand_pages(b):
    import random
    r, b = random.Random(3), bytearray(b)
    for _ in range(6):
        o = r.randrange(4096, len(b) - 4096)
        b[o:o + 512] = bytes(r.randrange(256) for _ in range(512))
    return bytes(b)


# Deterministic damage patterns. The SAME bytes are reported as CORRUPT (integrity_check returns
# rows) by some SQLite builds and UNREADABLE (integrity_check raises: "malformed", "string or blob
# too big", ...) by others, e.g. macOS/Python 3.9 vs 3.12. The contract is behavioural, so the
# tests accept either broken class but never OK/UNAVAILABLE, and never a crash.
CORRUPTIONS = {
    "mid_pages_ff": _mid_ff,
    "mid_pages_zero": _mid_zero,
    "random_pages": _rand_pages,
    "truncated": lambda b: b[: len(b) // 2],
    "bad_header": lambda b: b"not a database" * 8 + b[112:],
}


class TestIndexRecoveryContract(Base):
    """A broken derived index must never crash begin/search, must be detected by doctor, must be
    quarantined + rebuilt by doctor --repair, and must never cause canonical data to change."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        kdir = cls.p / "knowledge"
        kdir.mkdir(exist_ok=True)
        for i in range(200):
            (kdir / f"k{i}.md").write_text(f"# note {i}\n" + ("alpha beta gamma delta " * 60) + f"marmot{i}\n")

    def fresh_index(self):
        from aimem import index
        with core.lock("index"):
            for suf in ("", "-wal", "-shm"):
                Path(str(core.DB) + suf).unlink(missing_ok=True)
        index.reindex(None, quiet=True, full=True)
        with core.lock("index"):
            con = index.db_connect()
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        self.assertEqual(index.db_health(), index.DB_OK)
        self.assertGreater(core.DB.stat().st_size, 64 * 1024)

    def corrupt(self, fn):
        for suf in ("-wal", "-shm"):
            Path(str(core.DB) + suf).unlink(missing_ok=True)
        core.DB.write_bytes(fn(core.DB.read_bytes()))

    def snapshot(self, include=lambda rel: True):
        root = Path(core.ROOT) / "projects"
        return {str(f.relative_to(root)): core.sha256_file(f) for f in sorted(root.rglob("*"))
                if f.is_file() and include(f.relative_to(root))}

    def quarantined(self):
        return sorted(core.DB.parent.glob("memory.db.corrupt-*"))

    def test_doctor_detects_and_repairs_every_corruption_mode(self):
        from aimem import index
        import re
        for name, fn in CORRUPTIONS.items():
            with self.subTest(corruption=name):
                self.fresh_index()
                canonical = self.snapshot()
                q_before = len(self.quarantined())
                self.corrupt(fn)
                status = index.db_health()
                self.assertTrue(index.db_is_broken(status), f"{name}: undetected, db_health={status!r}")
                r = run("doctor", "--no-repo", "--slug", "hard")
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertRegex(r.stdout, r"ERROR: memory\.db BROKEN (CORRUPT|UNREADABLE):")
                r = run("doctor", "--no-repo", "--slug", "hard", "--repair")
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn("memory.db rebuilt (health OK)", r.stdout)
                self.assertNotIn("Traceback", r.stderr)
                self.assertGreater(len(self.quarantined()), q_before, "damaged file must be kept, not deleted")
                self.assertEqual(index.db_health(), index.DB_OK)
                self.assertTrue(index.fts_search("hard", "marmot7"))
                self.assertEqual(self.snapshot(), canonical, "repairing the index must not touch canonical data")
                self.assertIsNone(re.search(r"memory\.db", run("doctor", "--no-repo", "--slug", "hard").stdout))

    def test_begin_and_search_never_crash_on_any_corruption_mode(self):
        from aimem import index
        protected = lambda rel: rel.parts[1] in ("CURRENT.md", "NEXT.md", "project.json", "DECISIONS.jsonl",
                                                 "checkpoints", "knowledge") if len(rel.parts) > 1 else False
        for name, fn in CORRUPTIONS.items():
            with self.subTest(corruption=name):
                self.close_all()
                self.fresh_index()
                canonical = self.snapshot(protected)
                self.corrupt(fn)
                r = run("search", "hard", "marmot7")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                r = run("begin", "hard", "--agent", "other", "--task", f"after {name} marmot7")
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertNotIn("Traceback", r.stderr)
                self.close_all()
                # damage on pages no query touched may survive search; one doctor --repair always heals it
                if index.db_health() != index.DB_OK:
                    self.assertTrue(index.db_is_broken(index.db_health()))
                    self.assertEqual(run("doctor", "--no-repo", "--slug", "hard", "--repair").returncode, 0)
                self.assertEqual(index.db_health(), index.DB_OK)
                self.assertTrue(index.fts_search("hard", "marmot7"))
                self.assertEqual(self.snapshot(protected), canonical)

    def test_classification_is_explicit_and_portable(self):
        import sqlite3
        from aimem import index
        broken = [
            index.classify_integrity(exc=sqlite3.DataError("string or blob too big")),  # macOS / Python 3.9 CI
            index.classify_integrity(exc=sqlite3.DatabaseError("database disk image is malformed")),
            index.classify_integrity(exc=sqlite3.DatabaseError("file is not a database")),
            index.classify_integrity(exc=sqlite3.OperationalError("database disk image is malformed")),
            index.classify_integrity(rows=[("*** in database main ***",), ("Tree 8 page 3: btreeInitPage() returns error code 11",)]),
            index.classify_integrity(rows=[]),
            index.classify_integrity(exc=OSError("I/O error reading file")),
        ]
        for st in broken:
            self.assertTrue(index.db_is_broken(st), st)
            self.assertTrue(st.startswith(("CORRUPT:", "UNREADABLE:")), st)
        self.assertEqual(index.classify_integrity(rows=[("ok",)]), index.DB_OK)
        busy = index.classify_integrity(exc=sqlite3.OperationalError("database is locked"))
        self.assertTrue(busy.startswith(index.DB_UNAVAILABLE_PREFIX))
        self.assertFalse(index.db_is_broken(busy))
        self.assertFalse(index.db_is_broken(index.DB_MISSING))
        self.assertFalse(index.is_corruption_error(sqlite3.ProgrammingError("Cannot operate on a closed database.")))
        self.assertFalse(index.is_corruption_error(sqlite3.OperationalError("fts5: syntax error near \"\"")))
        self.assertTrue(index.is_corruption_error(sqlite3.DataError("string or blob too big")))

    def _integrity_check_raises(self, exc):
        """Real SQLite, except that PRAGMA integrity_check raises `exc` (reproduces a platform's error mode)."""
        import sqlite3
        from unittest import mock
        real = sqlite3.connect

        class Con:
            def __init__(self, c):
                self._c = c

            def execute(self, sql, *a):
                if "integrity_check" in sql:
                    raise exc
                return self._c.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._c, name)

        return mock.patch.object(sqlite3, "connect", side_effect=lambda *a, **k: Con(real(*a, **k)))

    def test_macos_py39_error_mode_is_detected_by_doctor_and_health(self):
        import sqlite3
        from aimem import doctor, health, index
        self.fresh_index()
        with self._integrity_check_raises(sqlite3.DataError("string or blob too big")):
            self.assertEqual(index.db_health(), "UNREADABLE:string or blob too big")
            errors, _w, _i = doctor.run(deep=False, check_repo=False, slug_filter="hard")
            self.assertTrue(any(e.startswith("memory.db BROKEN UNREADABLE:string or blob too big") for e in errors), errors)
            h = health.collect("hard", check_repo=False)
            self.assertEqual(h["AI_MEMORY_HEALTH"], "FAIL")
        self.assertEqual(index.db_health(), index.DB_OK)

    def test_busy_index_is_never_quarantined(self):
        import sqlite3
        from aimem import doctor, index
        self.fresh_index()
        inode = core.DB.stat().st_ino
        q_before = len(self.quarantined())
        with self._integrity_check_raises(sqlite3.OperationalError("database is locked")):
            errors, warnings, _i = doctor.run(deep=False, check_repo=False, repair=True, slug_filter="hard")
            self.assertFalse([e for e in errors if e.startswith("memory.db")], errors)
            self.assertTrue(any(w.startswith("memory.db UNAVAILABLE:") for w in warnings), warnings)
        self.assertEqual(core.DB.stat().st_ino, inode, "a busy index must not be moved aside")
        self.assertEqual(len(self.quarantined()), q_before)

    def test_committed_finish_survives_unusable_index(self):
        from aimem import index
        from unittest import mock
        import sqlite3
        self.close_all()
        sid = self.begin("finish with broken index")
        (self.p / "CURRENT.md").write_text((self.p / "CURRENT.md").read_text() + "\n- finish-index-test\n")
        with mock.patch.object(index, "_locked_refresh", side_effect=sqlite3.OperationalError("database is locked")):
            cp, version = sessions.finish("hard", sid, "PASS", "idx")
        self.assertTrue((self.p / "checkpoints" / cp / "meta.json").exists())
        self.assertEqual(core.project_manifest("hard")["current_checkpoint"], cp)
        self.assertEqual(json.loads(sessions.session_file("hard", sid).read_text())["status"], "CLOSED")


class TestTransactionReviewFindings(Base):
    """Regressions from the final adversarial review of transaction integrity."""

    def setUp(self):
        for t in txn.pending("hard"):
            txn.discard("hard", t["id"], force=True)
        self.cur, self.nxt = self.p / "CURRENT.md", self.p / "NEXT.md"

    def _torn_write_current(self):
        """A write-current killed after CURRENT.md was replaced but before NEXT.md (COMMITTING, half applied)."""
        t = txn.Transaction("hard", kind="write_current")
        t.stage(self.cur, "CUR-NEW\n")
        t.stage(self.nxt, "NEXT-NEW\n")
        t._record("PREPARED")
        t._record("COMMITTING")
        os.replace(t.entries[0]["staged"], t.entries[0]["target"])
        return t

    def test_discard_refuses_clean_roll_forward_and_never_deletes_content(self):
        t = txn.Transaction("hard")
        t.stage(self.nxt, "NEVER-APPLIED\n")
        t._record("PREPARED")
        before = self.nxt.read_text()
        r = run("txn", "hard", "--discard", t.id)
        self.assertEqual(r.returncode, 2)
        self.assertIn("REFUSED", r.stderr)
        self.assertEqual(len(txn.pending("hard")), 1)
        r = run("txn", "hard", "--discard", t.id, "--force")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"NEVER_APPLIED={self.nxt}", r.stdout)
        self.assertEqual(txn.pending("hard"), [])
        self.assertEqual(self.nxt.read_text(), before)
        q = self.p / "cold" / "quarantine" / f"txn-{t.id}"
        self.assertIn("NEVER-APPLIED", "".join(f.read_text() for f in q.iterdir() if f.name.endswith(".staged")))
        self.assertTrue((q / f"{t.id}.json").exists())
        self.assertEqual(run("txn", "hard", "--discard", "nope").returncode, 2)

    def test_repair_of_copied_root_repairs_the_copy_not_the_original(self):
        orig_next = self.nxt.read_text()
        t = self._torn_write_current()
        copy = _isolation.SANDBOX / "copied-root"
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(core.ROOT, copy, ignore=shutil.ignore_patterns(".locks"))
        env = dict(os.environ, AI_MEMORY_ROOT=str(copy), PYTHONPATH=str(REPO / "lib"))
        r = subprocess.run([sys.executable, str(AIMEM), "txn", "hard", "--repair"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("ROLLED_FORWARD", r.stdout)
        cp = copy / "projects" / "hard"
        self.assertEqual((cp / "NEXT.md").read_text(), "NEXT-NEW\n")
        self.assertEqual(self.nxt.read_text(), orig_next, "repair on a copy must never write into the original root")
        # legacy records (absolute paths only) on a copy fail closed instead
        rec_path = next((cp / ".txn").glob("*.json"), None)
        self.assertIsNone(rec_path)
        legacy = copy / "projects" / "hard" / ".txn" / "legacy.json"
        rec = json.loads(Path(t.record_path).read_text())
        for e in rec["entries"]:
            e.pop("target_rel"); e.pop("staged_rel")
        legacy.write_text(json.dumps(rec))
        r = subprocess.run([sys.executable, str(AIMEM), "txn", "hard", "--repair"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, core.EXIT_RECOVERY_REQUIRED, r.stdout)
        self.assertIn("PATHS_OUTSIDE_PROJECT_MANUAL_REVIEW", r.stdout)
        self.assertEqual(self.nxt.read_text(), orig_next)
        shutil.rmtree(copy)
        self.assertEqual(txn.repair("hard")[0][1], "ROLLED_FORWARD")  # the original's own txn still repairs

    def test_committing_entry_missing_staged_must_hold_new_content(self):
        t = self._torn_write_current()
        self.cur.write_text("EDITED AFTER CRASH\n")  # the applied target was changed since
        self.assertEqual(txn.inspect("hard")[0]["resolution"], "MANUAL_TARGET_CHANGED")
        r = run("txn", "hard", "--repair")
        self.assertEqual(r.returncode, core.EXIT_RECOVERY_REQUIRED, "manual leftovers keep exit 4")
        self.assertIn("TARGET_CHANGED_SINCE_PREPARE_MANUAL_REVIEW", r.stdout)
        self.assertEqual(self.cur.read_text(), "EDITED AFTER CRASH\n")
        txn.discard("hard", t.id, force=True)

    def test_idempotent_write_torn_mid_commit_still_rolls_forward(self):
        # the staged content equals the current content (pre == new): the applied entry holds "both"
        self.cur.write_text("SAME\n")
        t = txn.Transaction("hard")
        t.stage(self.cur, "SAME\n")
        t.stage(self.nxt, "NEXT-AFTER\n")
        t._record("PREPARED")
        t._record("COMMITTING")
        os.replace(t.entries[0]["staged"], t.entries[0]["target"])
        self.assertEqual(txn.inspect("hard")[0]["resolution"], "ROLL_FORWARD")
        self.assertEqual(txn.repair("hard")[0][1], "ROLLED_FORWARD")
        self.assertEqual(self.nxt.read_text(), "NEXT-AFTER\n")

    def test_prepared_with_changed_target_rolls_back_safely(self):
        t = txn.Transaction("hard")
        t.stage(self.nxt, "STALE\n")
        t._record("PREPARED")
        self.nxt.write_text("NEWER\n")
        self.assertEqual(txn.inspect("hard")[0]["resolution"], "ROLL_BACK")
        self.assertEqual(txn.repair("hard")[0][1], "ROLLED_BACK")
        self.assertEqual(self.nxt.read_text(), "NEWER\n")
        self.assertEqual(txn.pending("hard"), [])

    def test_malformed_records_never_crash_and_can_be_discarded(self):
        d = self.p / ".txn"
        (d / "bad-list.json").write_text("[]")
        (d / "bad-entry.json").write_text(json.dumps({"state": "COMMITTING", "entries": [{"target": "x"}]}))
        for args in (("txn", "hard"), ("txn", "hard", "--repair"), ("doctor", "--no-repo", "--slug", "hard")):
            r = run(*args)
            self.assertNotIn("Traceback", r.stderr, args)
        self.assertEqual(run("txn", "hard").returncode, core.EXIT_RECOVERY_REQUIRED)
        self.assertEqual({t["resolution"] for t in txn.inspect("hard")}, {"MANUAL_CORRUPT_RECORD"})
        r = run("begin", "hard", "--agent", "other", "--task", "malformed txn")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.close_all()
        for rid in ("bad-list", "bad-entry"):
            self.assertEqual(run("txn", "hard", "--discard", rid).returncode, 0)
        self.assertEqual(txn.pending("hard"), [])

    def test_register_update_respects_pending_transactions_and_lock(self):
        t = self._torn_write_current()
        man = (self.p / "project.json").read_bytes()
        r = run("register", "hard", "--name", "Renamed", "--repo", str(self.repo), "--update")
        self.assertEqual(r.returncode, core.EXIT_RECOVERY_REQUIRED, r.stderr)
        self.assertEqual((self.p / "project.json").read_bytes(), man)
        self.assertEqual(txn.inspect("hard")[0]["resolution"], "ROLL_FORWARD", "register must not turn repair into MANUAL")
        txn.repair("hard")
        with core.project_write_lock("hard"):
            r = subprocess.run([sys.executable, str(AIMEM), "register", "hard", "--name", "R", "--repo", str(self.repo), "--update"],
                               capture_output=True, text=True, timeout=60,
                               env=dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT), PYTHONPATH=str(REPO / "lib")))
        self.assertIn("LOCK_TIMEOUT", r.stderr) if r.returncode == core.EXIT_LOCK_TIMEOUT else self.assertEqual(r.returncode, 0)

    def test_doctor_repair_reports_post_repair_state(self):
        self._torn_write_current()
        r = run("doctor", "--no-repo", "--slug", "hard", "--repair")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("unresolved transaction", r.stdout)
        self.assertIn("ROLLED_FORWARD", r.stdout)

    def test_migrate_refuses_while_transactions_pending(self):
        from aimem import migrate
        t = self._torn_write_current()
        man = (self.p / "project.json").read_bytes()
        with self.assertRaises(SystemExit) as cm:
            migrate.plan_and_apply()
        self.assertEqual(cm.exception.code, core.EXIT_RECOVERY_REQUIRED)
        self.assertEqual((self.p / "project.json").read_bytes(), man)
        txn.repair("hard")

    def test_version_build_suffixes_are_never_downgraded(self):
        for v in ("4.3.0", "5", "4.2.0+hotfix2", "4.2.0rc1", "4.2.0.1", "garbage", ""):
            self.assertTrue(core.version_not_older(v, "4.2.0"), v)
        for v in ("4.1.0", "V3.1.1", "2.0.0+V3.1.1", "4.2"):
            self.assertFalse(core.version_not_older(v, "4.2.0"), v)


class TestIndexReviewFindings(Base):
    def test_walk_survives_directories_vanishing(self):
        from aimem import index
        root = _isolation.SANDBOX / "walk"
        shutil.rmtree(root, ignore_errors=True)
        for d in ("a", "b", "c"):
            (root / d).mkdir(parents=True)
            for i in range(5):
                (root / d / f"{i}.md").write_text("x")
        gen = index._walk_files(root)
        first = next(gen)
        shutil.rmtree(root / "b")
        rest = list(gen)
        self.assertTrue(first.exists())
        self.assertFalse([f for f in rest if "/b/" in str(f)])
        shutil.rmtree(root)

    def test_recovery_never_blocks_behind_index_lock(self):
        import time
        from aimem import index
        run("note", "hard", "--kind", "decision", "--text", "choose ocelotplan", check=True)
        index.reindex(None, quiet=True, full=True)
        for suf in ("-wal", "-shm"):
            Path(str(core.DB) + suf).unlink(missing_ok=True)
        core.DB.write_bytes(b"garbage" * 5000)
        holder = subprocess.Popen([sys.executable, "-c",
                                   "import time\nfrom aimem import core\nwith core.lock('index', timeout=5):\n"
                                   "    print('HELD', flush=True)\n    time.sleep(60)"],
                                  stdout=subprocess.PIPE, text=True,
                                  env=dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT), PYTHONPATH=str(REPO / "lib")))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "HELD")
            t0 = time.time()
            r = run("begin", "hard", "--agent", "other", "--task", "ocelotplan while index busy")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertLess(time.time() - t0, 40)
        finally:
            holder.kill()
            holder.wait()
        self.close_all() if hasattr(self, "close_all") else None
        run("doctor", "--no-repo", "--slug", "hard", "--repair")
        self.assertEqual(index.db_health(), index.DB_OK)

    def test_doctor_never_quarantines_an_index_healed_concurrently(self):
        from unittest import mock
        from aimem import doctor, index
        index.reindex(None, quiet=True, full=True)
        q_before = sorted(core.DB.parent.glob("memory.db.corrupt-*"))
        real = index.db_health
        calls = {"n": 0}

        def health():
            calls["n"] += 1
            return "CORRUPT:simulated" if calls["n"] == 1 else real()  # healed before doctor takes the lock
        with mock.patch.object(index, "db_health", side_effect=health):
            _e, _w, info = doctor.run(check_repo=False, repair=True, slug_filter="hard")
        self.assertEqual(sorted(core.DB.parent.glob("memory.db.corrupt-*")), q_before)
        self.assertIn("memory.db already healthy (healed concurrently)", info)

    def test_missing_index_table_is_detected_and_repaired(self):
        from aimem import index
        index.reindex(None, quiet=True, full=True)
        with core.lock("index"):
            con = index.db_connect()
            con.execute("DROP TABLE chunks_fts")
            con.commit()
            con.close()
        self.assertTrue(index.db_is_broken(index.db_health()))
        r = run("doctor", "--no-repo", "--slug", "hard")
        self.assertIn("schema missing table(s) chunks_fts", r.stdout)
        self.assertEqual(run("doctor", "--no-repo", "--slug", "hard", "--repair").returncode, 0)
        self.assertEqual(index.db_health(), index.DB_OK)


if __name__ == "__main__":
    unittest.main()
