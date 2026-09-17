"""aimem selftest [--full]: end-to-end tests in an ISOLATED temporary memory root.

Everything runs through subprocesses with AI_MEMORY_ROOT pointed at a temp root and a
throwaway git repository, so the live memory and real project repos are never touched.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "bin"
PY = sys.executable


class T:
    def __init__(self, full=False, keep=False, verbose=False):
        self.full, self.keep, self.verbose = full, keep, verbose
        self.tmp = Path(tempfile.mkdtemp(prefix="aimem-selftest-"))
        self.root = self.tmp / "memory"
        self.repo = self.tmp / "repo"
        self.repo2 = self.tmp / "repo2"
        self.bk = self.tmp / "backups"
        self.results = []
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(self.root), AIMEM_SELFTEST="1")
        self.env.pop("AIMEM_SESSION_ID", None)

    # ------------------------------------------------------------ helpers
    def run(self, *args, rc=None, timeout=120, env=None, input_text=None):
        cmd = [PY, str(BIN / args[0])] + [str(a) for a in args[1:]]
        r = subprocess.run(cmd, env=env or self.env, capture_output=True, text=True, timeout=timeout, input=input_text)
        out = r.stdout + r.stderr
        if self.verbose:
            print(f"$ {' '.join(args)}\n{out.strip()}\n[rc={r.returncode}]")
        if rc is not None and r.returncode != rc:
            raise AssertionError(f"{' '.join(str(a) for a in args)} -> rc={r.returncode} expected {rc}\n{out[-2500:]}")
        return r.returncode, out

    def py(self, code, rc=0):
        r = subprocess.run([PY, "-c", code], env=dict(self.env, PYTHONPATH=str(BIN.parent / "lib")), capture_output=True, text=True, timeout=120)
        if rc is not None and r.returncode != rc:
            raise AssertionError(f"python snippet rc={r.returncode}\n{r.stdout}{r.stderr}")
        return r.returncode, r.stdout + r.stderr

    def git(self, repo, *a):
        return subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, text=True, timeout=30)

    def val(self, out, key):
        for line in out.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
        return None

    def check(self, name, fn):
        t0 = time.time()
        try:
            fn()
            self.results.append((name, "PASS", "", time.time() - t0))
            print(f"PASS  {name}")
        except Exception as e:  # noqa: BLE001
            self.results.append((name, "FAIL", str(e)[-1500:], time.time() - t0))
            print(f"FAIL  {name}\n      {str(e)[-1500:]}")

    # ------------------------------------------------------------ setup
    def make_repo(self, path, n=1):
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init", "-q")
        self.git(path, "config", "user.email", "t@t")
        self.git(path, "config", "user.name", "t")
        (path / "README.md").write_text("hello\n")
        self.git(path, "add", ".")
        self.git(path, "commit", "-qm", f"c{n}")
        return self.git(path, "rev-parse", "HEAD").stdout.strip()

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-qm", f"add {name}")
        return self.git(repo, "rev-parse", "HEAD").stdout.strip()

    def set_current(self, slug, text):
        (self.root / "projects" / slug / "CURRENT.md").write_text(text)

    # ------------------------------------------------------------ tests
    def t_init(self):
        rc, out = self.run("aimem", "init", str(self.root), rc=0)
        assert (self.root / "registry" / "projects.json").exists()
        cfg = json.loads((self.root / "config.json").read_text())
        cfg["lock_timeout_s"] = 2
        cfg["backup_dir"] = str(self.bk)
        cfg["dashboard_port"] = 0
        (self.root / "config.json").write_text(json.dumps(cfg, indent=2))
        rc, out = self.run("aimem", "version", rc=0)
        assert "aimem 4.1." in out

    def t_register(self):
        self.make_repo(self.repo)
        self.run("aimem", "register", "alpha", "--name", "Alpha", "--repo", str(self.repo), rc=0)
        self.run("aimem", "register", "beta", "--name", "Beta", rc=0)
        rc, out = self.run("aimem", "register", "bad", "--repo", str(self.root))
        assert rc != 0 and "RECURSIVE_INDEX_RISK" in out, out
        rc, out = self.run("aimem", "register", "alpha")
        assert rc != 0, "duplicate registration must fail"

    def t_detect(self):
        rc, out = self.run("aimem-detect", str(self.repo / "sub" / "dir"), rc=0)
        assert out.strip() == "alpha", out
        rc, out = self.run("aimem-detect", str(self.tmp))
        assert rc == 1

    def t_begin(self):
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "claude", "--task", "first task", "--tokens", "3000", rc=0)
        self.sid = self.val(out, "SESSION_ID")
        assert self.sid and self.val(out, "CONTEXT") and Path(self.val(out, "CONTEXT")).exists()
        assert self.val(out, "RECONCILIATION") in ("MEMORY_AHEAD_OR_UNVERIFIED", "MEMORY_MATCH"), out
        tokens = int(self.val(out, "APPROX_TOKENS"))
        assert tokens <= 3000 + 200, f"context exceeded budget: {tokens}"
        ctx = Path(self.val(out, "CONTEXT")).read_text()
        assert "GENERATED AI CONTEXT PACK" in ctx and "CURRENT AUTHORITATIVE STATE" in ctx

    def t_step_binding(self):
        self.run("aimem-step", "--project", "alpha", "--session", self.sid, "--agent", "claude", "--kind", "read", "--summary", "read files", rc=0)
        rc, out = self.run("aimem-step", "--project", "alpha", "--session", self.sid, "--kind", "write", "--summary", "wrote x " + "password" + "=hunter22", "--file", "x.py", "--result", "PASS", rc=0)
        assert "binding=explicit" in out
        steps = (self.root / "projects" / "alpha" / "sessions" / f"{self.sid}.steps.jsonl").read_text().splitlines()
        assert len(steps) == 2
        assert "hunter22" not in steps[1] and "[REDACTED_SECRET]" in steps[1], "secret must be redacted"
        assert all(json.loads(s)["session_id"] == self.sid for s in steps)
        rc, out = self.run("aimem-step", "--project", "alpha", "--session", "nope-123", "--kind", "read", "--summary", "x")
        assert rc != 0
        j = json.loads((self.root / "projects" / "alpha" / "sessions" / f"{self.sid}.json").read_text())
        assert j["lease"]["heartbeats"] >= 2, "steps must refresh heartbeat"

    def t_heartbeat(self):
        rc, out = self.run("aimem", "heartbeat", "alpha", "--session", self.sid, "--note", "long build running", rc=0)
        assert "HEARTBEAT=OK" in out
        rc, out = self.run("aimem", "sessions", "alpha", "--open", rc=0)
        assert "OPEN/ACTIVE" in out

    def t_finish_unchanged_refused(self):
        rc, out = self.run("aimem", "finish", "alpha", "--session", self.sid, "--result", "PASS")
        assert rc != 0 and "did not change" in out

    def t_finish(self):
        self.set_current("alpha", "# Alpha — CURRENT\nSTATUS: PHASE1_DONE\nHEAD: " + self.git(self.repo, "rev-parse", "HEAD").stdout.strip() + "\n## Exact next action\n- phase 2\n")
        rc, out = self.run("aimem", "finish", "alpha", "--session", self.sid, "--result", "PASS", "--label", "phase-1", "--summary", "done", rc=0)
        self.cp1 = self.val(out, "CHECKPOINT")
        assert self.cp1 and "FINISH=PASS" in out
        man = json.loads((self.root / "projects" / "alpha" / "project.json").read_text())
        assert man["current_checkpoint"] == self.cp1 and man["memory_version"] >= 1
        meta = json.loads((self.root / "projects" / "alpha" / "checkpoints" / self.cp1 / "meta.json").read_text())
        assert meta["session_id"] == self.sid and meta["result"] == "PASS"
        prov = (self.root / "projects" / "alpha" / "PROVENANCE.jsonl").read_text()
        assert '"key": "checkpoint.current"' in prov and '"source_type": "PHYSICAL_GIT"' in prov
        rc, out = self.run("aimem", "finish", "alpha", "--session", self.sid, "--result", "PASS")
        assert rc != 0, "closed session must not finish twice"

    def t_checkpoint_no_advance(self):
        rc, out = self.run("aimem", "checkpoint", "alpha", "--label", "admin-readonly", "--result", "PASS", "--no-advance-current", rc=0)
        man = json.loads((self.root / "projects" / "alpha" / "project.json").read_text())
        assert man["current_checkpoint"] == self.cp1, "admin checkpoint must not advance semantic current"
        rc, out = self.run("aimem", "checkpoint", "alpha", "--label", "semantic", "--result", "PASS", rc=0)
        man = json.loads((self.root / "projects" / "alpha" / "project.json").read_text())
        assert man["current_checkpoint"] != self.cp1
        self.cp2 = man["current_checkpoint"]

    def t_search_reindex(self):
        self.run("aimem", "reindex", rc=0)
        rc, out = self.run("aimem", "search", "alpha", "PHASE1_DONE", rc=0)
        assert "CURRENT.md" in out and "NO_MATCHES" not in out
        rc, out = self.run("aimem", "search", "beta", "PHASE1_DONE", rc=0)
        assert "NO_MATCHES" in out, "project scope must be enforced"
        rc, out = self.run("aimem", "search", "alpha", "phase 2", rc=0)
        first = out.splitlines()[0]
        assert "hot_current" in first or "hot_next" in first or "checkpoint" in first, f"hot docs should rank first: {first}"

    def t_write_current_cas(self):
        f = self.tmp / "cur.md"
        f.write_text("# Alpha — CURRENT\nSTATUS: CAS_TEST\n")
        man = json.loads((self.root / "projects" / "alpha" / "project.json").read_text())
        v = man["memory_version"]
        rc, out = self.run("aimem", "write-current", "alpha", "--current", str(f), "--expect-version", str(v + 5))
        assert rc == 3 and "VERSION_CONFLICT" in out, out
        rc, out = self.run("aimem", "write-current", "alpha", "--current", str(f), "--expect-version", str(v), rc=0)
        assert "CAS_TEST" in (self.root / "projects" / "alpha" / "CURRENT.md").read_text()
        f.write_text("# Alpha\nAPI_KEY: " + "sk" + "-" + "abcdefghijklmnopqrstuvwxyz123456\n")
        rc, out = self.run("aimem", "write-current", "alpha", "--current", str(f))
        assert rc != 0 and "SECRET_PATTERN_REJECTED" in out

    def t_txn_interruption(self):
        p = self.root / "projects" / "alpha"
        before = (p / "CURRENT.md").read_text()
        code = f"""
import sys; sys.path.insert(0, {str(BIN.parent / 'lib')!r})
from aimem import txn, core
from pathlib import Path
t = txn.Transaction('alpha', kind='simulated_crash')
t.stage(Path({str(p / 'CURRENT.md')!r}), '# Alpha\\nSTATUS: AFTER_CRASH_RECOVERY\\n')
t.stage(Path({str(p / 'NEXT.md')!r}), '# NEXT\\nrecovered\\n')
t._record('PREPARED'); t._record('COMMITTING')
import os; os.replace(t.entries[0]['staged'], t.entries[0]['target'])   # first file applied, then crash
print('CRASHED_MID_COMMIT')
"""
        rc, out = self.py(code)
        assert "CRASHED_MID_COMMIT" in out
        assert "AFTER_CRASH_RECOVERY" in (p / "CURRENT.md").read_text()
        assert "recovered" not in (p / "NEXT.md").read_text()
        rc, out = self.run("aimem", "txn", "alpha")
        assert rc == 4 and "COMMITTING" in out and "ROLL_FORWARD" in out, out
        rc, out = self.run("aimem", "doctor")
        assert rc == 1 and "unresolved transaction" in out
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "codex", "--task", "detect txn", rc=0)
        assert "UNRESOLVED_TRANSACTIONS=1" in out
        self.run("aimem", "session", "close", "alpha", "--session", self.val(out, "SESSION_ID"), "--result", "ABANDONED", rc=0)
        rc, out = self.run("aimem", "txn", "alpha", "--repair", rc=0)
        assert "ROLLED_FORWARD" in out
        assert "recovered" in (p / "NEXT.md").read_text()
        rc, out = self.run("aimem", "txn", "alpha", rc=0)
        assert "NO_PENDING_TRANSACTIONS" in out
        # PREPARED with missing staged file -> rolled back
        code2 = f"""
import sys; sys.path.insert(0, {str(BIN.parent / 'lib')!r})
from aimem import txn
from pathlib import Path
t = txn.Transaction('alpha', kind='simulated_prepared_crash')
t.stage(Path({str(p / 'NEXT.md')!r}), 'SHOULD_NOT_APPLY')
t._record('PREPARED')
import os; os.unlink(t.entries[0]['staged'])
"""
        self.py(code2)
        rc, out = self.run("aimem", "txn", "alpha", "--repair", rc=0)
        assert "ROLLED_BACK" in out and "SHOULD_NOT_APPLY" not in (p / "NEXT.md").read_text()
        self.run("aimem", "doctor", "--deep", rc=0)

    def t_locking(self):
        holder = subprocess.Popen([PY, "-c", f"""
import sys, time; sys.path.insert(0, {str(BIN.parent / 'lib')!r})
from aimem import core
with core.project_write_lock('alpha', timeout=5):
    print('HELD', flush=True); time.sleep(4)
"""], env=self.env, stdout=subprocess.PIPE, text=True)
        assert holder.stdout.readline().strip() == "HELD"
        t0 = time.time()
        rc, out = self.run("aimem", "checkpoint", "alpha", "--label", "blocked", "--result", "PASS")
        holder.wait()
        assert rc == 5 and "LOCK_TIMEOUT" in out, f"rc={rc} {out}"
        assert time.time() - t0 < 4.5
        self.run("aimem", "checkpoint", "beta", "--label", "other-project-unaffected", "--result", "PASS", rc=0)

    def t_two_sessions_cas(self):
        rc, oa = self.run("aimem", "begin", "alpha", "--agent", "claude", "--task", "A", rc=0)
        rc, ob = self.run("aimem", "begin", "alpha", "--agent", "codex", "--task", "B", rc=0)
        a, b = self.val(oa, "SESSION_ID"), self.val(ob, "SESSION_ID")
        assert "WARN=OTHER_OPEN_SESSION_EXISTS" in ob
        # ambiguous: no --session must fail closed
        rc, out = self.run("aimem-step", "--project", "alpha", "--kind", "read", "--summary", "ambiguous")
        assert rc != 0 and "AMBIGUOUS_OPEN_SESSIONS" in out
        rc, out = self.run("aimem", "finish", "alpha", "--result", "PASS", "--allow-unchanged")
        assert rc == 6 and "AMBIGUOUS" in out, out
        self.run("aimem-step", "--project", "alpha", "--session", a, "--kind", "read", "--summary", "A reads", rc=0)
        self.run("aimem-step", "--project", "alpha", "--session", b, "--kind", "read", "--summary", "B reads", rc=0)
        sa = (self.root / "projects" / "alpha" / "sessions" / f"{a}.steps.jsonl").read_text()
        sb = (self.root / "projects" / "alpha" / "sessions" / f"{b}.steps.jsonl").read_text()
        assert "B reads" not in sa and "A reads" not in sb, "cross-session leakage"
        self.set_current("alpha", "# Alpha\nSTATUS: B_WROTE\n")
        rc, out = self.run("aimem", "finish", "alpha", "--session", b, "--result", "PASS", "--label", "B", rc=0)
        self.set_current("alpha", "# Alpha\nSTATUS: A_WROTE_WITHOUT_SEEING_B\n")
        rc, out = self.run("aimem", "finish", "alpha", "--session", a, "--result", "PASS", "--label", "A")
        assert rc == 3 and "CANONICAL_STATE_ADVANCED_BY_ANOTHER_SESSION" in out, out
        man = json.loads((self.root / "projects" / "alpha" / "project.json").read_text())
        rc, out = self.run("aimem", "finish", "alpha", "--session", a, "--result", "PASS", "--label", "A-merged", "--expect-version", str(man["memory_version"]), rc=0)

    def t_stale_memory(self):
        rc, out = self.run("aimem", "reconcile", "alpha", rc=0)
        assert "STATUS: MEMORY_MATCH" in out
        self.commit(self.repo, "new.txt", "physical work\n")
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "claude", "--task", "after physical change", rc=0)
        assert self.val(out, "RECONCILIATION") == "PHYSICAL_AHEAD", out
        assert Path(self.val(out, "RECONCILE_CONTEXT")).exists()
        sid = self.val(out, "SESSION_ID")
        (self.repo / "dirty.txt").write_text("x")
        rc, out2 = self.run("aimem", "reconcile", "alpha")
        assert rc == 1 and "DIRTY_REPO" in out2
        (self.repo / "dirty.txt").unlink()
        self.set_current("alpha", "# Alpha\nSTATUS: RECONCILED\nHEAD: " + self.git(self.repo, "rev-parse", "HEAD").stdout.strip() + "\n")
        self.run("aimem", "finish", "alpha", "--session", sid, "--result", "PASS", "--label", "reconciled", rc=0)
        rc, out = self.run("aimem", "reconcile", "alpha", rc=0)
        # MEMORY_AHEAD: check out an older commit
        rc, out = self.run("aimem", "begin", "beta", "--agent", "other", "--task", "no repo", rc=0)
        assert self.val(out, "RECONCILIATION") == "RUNTIME_VERIFICATION_REQUIRED"
        self.run("aimem", "session", "close", "beta", "--session", self.val(out, "SESSION_ID"), rc=0)

    def t_abandoned_recovery(self):
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "codex", "--task", "will crash", rc=0)
        sid = self.val(out, "SESSION_ID")
        self.run("aimem-step", "--project", "alpha", "--session", sid, "--kind", "write", "--summary", "edited a.py", "--file", "a.py", "--result", "PASS", rc=0)
        self.run("aimem-step", "--project", "alpha", "--session", sid, "--kind", "test", "--summary", "ran tests", "--result", "FAIL", rc=0)
        self.run("aimem-step", "--project", "alpha", "--session", sid, "--kind", "error", "--summary", "test failed on b.py", rc=0)
        self.commit(self.repo, "a.py", "print(1)\n")
        rc, out = self.run("aimem", "recover", "alpha", rc=0)
        assert "OPEN_OK" in out and "RECOVERY_REQUIRED_COUNT=0" in out, "active session must not be flagged"
        sf = self.root / "projects" / "alpha" / "sessions" / f"{sid}.json"
        j = json.loads(sf.read_text())
        j["lease"]["last_heartbeat"] = "2020-01-01T00:00:00+00:00"
        sf.write_text(json.dumps(j))
        rc, out = self.run("aimem", "sessions", "alpha", "--open", rc=0)
        assert "OPEN/ABANDONED" in out
        rc, out = self.run("aimem", "recover", "alpha")
        assert rc == 4 and "RECOVERY_REQUIRED" in out and "RECOVERY_REQUIRED_COUNT=1" in out, out
        rc, out = self.run("aimem", "recover", "--session", sid)
        assert rc == 4
        assert "a.py" in out and "test failed on b.py" in out and "Must NOT be repeated" in out and "edited a.py" in out
        assert "ran tests" in out and "FAIL" in out
        rc, out = self.run("aimem", "health", "alpha")
        assert rc == 4 and "recovery_required=1" in out
        self.run("aimem", "session", "close", "alpha", "--session", sid, "--result", "ABANDONED", "--note", "simulated crash", rc=0)
        rc, out = self.run("aimem", "recover", "alpha", rc=0)

    def t_compaction(self):
        rc, out = self.run("aimem", "compact", "alpha", "--dry-run", rc=0)
        assert "COMPACT=DRY_RUN" in out
        assert not (self.root / "projects" / "alpha" / "cold" / "summaries").exists()
        rc, out = self.run("aimem", "compact", "--all", rc=0)
        sd = self.root / "projects" / "alpha" / "cold" / "summaries"
        daily = list((sd / "daily").glob("*.md"))
        phase = list((sd / "phase").glob("*.md"))
        assert daily and phase and (sd / "CHECKPOINTS.md").exists()
        raw = list((self.root / "projects" / "alpha" / "cold" / "step-journal").glob("*.jsonl"))
        assert raw, "raw steps must never be deleted"
        first = daily[0].read_text()
        rc, out = self.run("aimem", "compact", "alpha", rc=0)
        assert "daily_written=0" in out, "unchanged sources must be skipped (deterministic/idempotent)"
        rc, out = self.run("aimem", "compact", "alpha", "--rebuild", rc=0)
        second = daily[0].read_text()
        strip = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("GENERATED:"))
        assert strip(first) == strip(second), "rebuild must be deterministic"

    def t_artifact_dedup(self):
        f1 = self.tmp / "big1.bin"
        f2 = self.tmp / "big2.bin"
        f1.write_bytes(b"A" * 200000)
        f2.write_bytes(b"A" * 200000)
        rc, o1 = self.run("aimem", "artifact", "alpha", "--path", str(f1), "--store", rc=0)
        rc, o2 = self.run("aimem", "artifact", "beta", "--path", str(f2), "--store", "--note", "same content", rc=0)
        j1, j2 = json.loads(o1), json.loads(o2)
        assert j1["object"] == j2["object"] and j1["deduplicated"] is False and j2["deduplicated"] is True
        obj = Path(j1["stored_path"])
        assert obj.exists() and obj.parent.name == j1["object"][2:4] and obj.parent.parent.name == j1["object"][:2]
        rc, out = self.run("aimem", "objects", "stat", rc=0)
        assert "OBJECTS=1" in out, out
        rc, out = self.run("aimem", "objects", "verify", rc=0)
        assert "OBJECTS_VERIFY=PASS" in out
        os.chmod(obj, 0o644)
        obj.write_bytes(b"B")
        rc, out = self.run("aimem", "objects", "verify")
        assert rc == 1 and "MISMATCH" in out
        rc, out = self.run("aimem", "doctor", "--deep")
        assert rc == 1 and "object hash mismatch" in out
        obj.write_bytes(b"A" * 200000)
        os.chmod(obj, 0o444)
        self.run("aimem", "objects", "verify", rc=0)

    def t_chat_export(self):
        rc, out = self.run("aimem", "chat-export", "alpha", "--query", "phase 2", "--tokens", "2500", rc=0)
        f = Path(self.val(out, "CHAT_EXPORT"))
        t = f.read_text()
        assert "AI_MEMORY_CHATGPT_CONTEXT_V1" in t and "do NOT have access to this Mac's filesystem" in t and "Do not invent" in t
        assert "EXACT_NEXT_ACTION" in t and "PROJECT_SLUG: alpha" in t
        assert int(self.val(out, "APPROX_TOKENS")) < 4000

    def t_chat_import(self):
        head = self.git(self.repo, "rev-parse", "HEAD").stdout.strip()
        good = self.tmp / "packet_good.md"
        good.write_text(f"""================ AI_MEMORY_AGENT_PACKET BEGIN ================
PACKET_VERSION:
AI_MEMORY_AGENT_PACKET_V1
SOURCE:
CHATGPT_EXISTING_CHAT
PROJECT_SLUG:
alpha
CURRENT_AUTHORITATIVE_STATE
* Branch: `main`
* HEAD: `{head}`
EXACT_NEXT_ACTION
do the thing
================ AI_MEMORY_AGENT_PACKET END =================
""")
        bad = self.tmp / "packet_bad.md"
        bad.write_text(good.read_text().replace(head, "0" * 40).replace("alpha", "gamma"))
        cur_before = (self.root / "projects" / "alpha" / "CURRENT.md").read_text()
        rc, out = self.run("aimem", "chat-import", "alpha", str(good), "--dry-run", rc=0)
        assert "CHAT_IMPORT=IMPORTED" in out and "dry_run=True" in out
        assert not list((self.root / "projects" / "alpha" / "knowledge").glob("chat-imports/*")), "dry-run must not store"
        rc, out = self.run("aimem", "chat-import", "alpha", str(bad), "--dry-run")
        assert rc == 3 and "PACKET_SLUG_MISMATCH" in out and "PHYSICAL_HEAD_NOT_IN_PACKET" in out, out
        rc, out = self.run("aimem", "chat-import", "alpha", str(good), rc=0)
        stored = self.val(out, "STORED").split(" ")[0]
        assert Path(stored).read_text() == good.read_text(), "packet must be preserved verbatim"
        rc, out = self.run("aimem", "chat-import", "alpha", str(bad))
        assert rc == 3, "conflicts must fail closed"
        assert (self.root / "projects" / "alpha" / "CURRENT.md").read_text() == cur_before, "import must never touch CURRENT.md"
        rc, out = self.run("aimem", "fact", "list", "alpha", rc=0)
        assert "chatgpt.claimed_head" in out and "REPORTED" in out
        # secret in packet -> fail closed
        sec = self.tmp / "packet_secret.md"
        sec.write_text(good.read_text().replace("do the thing", "token: " + "ghp" + "_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"))
        rc, out = self.run("aimem", "chat-import", "alpha", str(sec), "--dry-run")
        assert rc == 3 and "SECRET_PATTERN" in out

    def t_provenance(self):
        rc, out = self.run("aimem", "fact", "set", "alpha", "--key", "runtime.db_rows", "--value", "42", "--source-type", "DATABASE_QUERY",
                           "--source-ref", "sqlite:main.db", "--status", "VERIFIED", rc=0)
        rc, out = self.run("aimem", "fact", "set", "alpha", "--key", "x", "--value", "1", "--source-type", "BOGUS")
        assert rc != 0
        rc, out = self.run("aimem", "fact", "list", "alpha", "--key", "runtime.db_rows", rc=0)
        assert "42\tVERIFIED\tDATABASE_QUERY" in out
        self.commit(self.repo, "z.txt", "z\n")
        rc, out = self.run("aimem", "fact", "list", "alpha", "--key", "repo.head", rc=0)
        assert "STALE" in out, "physical fact must show STALE after repo moved"
        rc, out = self.run("aimem", "doctor", "--deep", rc=0)
        assert "STALE" in out
        self.run("aimem", "capture", "alpha", rc=0)
        rc, out = self.run("aimem", "fact", "list", "alpha", "--key", "repo.head", rc=0)
        assert "VERIFIED" in out and "STALE" not in out

    def t_health(self):
        rc, out = self.run("aimem", "health", "--verbose")
        assert rc in (0, 1), out
        for k in ("AI_MEMORY_HEALTH=", "version=4.1.", "projects=2", "active_sessions=", "database=", "backup_status=", "disk_usage=", "unresolved_transactions=0", "recovery_required=0"):
            assert k in out, f"missing {k}: {out}"
        rc, out = self.run("aimem", "health", "--json", rc=None)
        json.loads(out)

    def t_backup_restore(self):
        rc, out = self.run("aimem", "backup", "--label", "selftest", rc=0)
        path = Path(out.split()[-1])
        assert path.exists() and Path(str(path) + ".sha256").exists(), "sidecar must be <name>.tar.gz.sha256"
        rc, out = self.run("aimem", "backups", rc=0)
        assert "selftest" in out and "verified=NO" in out
        rc, out = self.run("aimem", "restore", str(path), "--verify", rc=0)
        assert "RESTORE_VERIFY=PASS" in out and "manifest_hashes: PASS" in out and "doctor_on_restored_copy: PASS" in out, out
        rc, out = self.run("aimem", "backups", rc=0)
        assert "verified=PASS" in out
        # tamper -> FAIL
        tampered = self.tmp / "tampered.tar.gz"
        shutil.copy2(path, tampered)
        with tampered.open("r+b") as f:
            f.seek(100)
            f.write(b"\x00\x00\x00\x00")
        shutil.copy2(Path(str(path) + ".sha256"), Path(str(tampered) + ".sha256"))
        rc, out = self.run("aimem", "restore", str(tampered), "--verify")
        assert rc == 1 and "RESTORE_VERIFY=FAIL" in out
        rc, out = self.run("aimem", "restore", str(path))
        assert rc == 1 and "--confirm" in out, "restore must not mutate without --confirm"
        marker = self.root / "MARKER_AFTER_BACKUP"
        marker.write_text("x")
        rc, out = self.run("aimem", "restore", str(path), "--confirm", rc=0)
        assert not marker.exists() and "previous_root_moved_to=" in out
        aside = Path(out.split("previous_root_moved_to=")[1].strip().splitlines()[0])
        assert (aside / "MARKER_AFTER_BACKUP").exists(), "previous root must be preserved, not deleted"
        self.run("aimem", "doctor", "--deep", rc=0)
        rc, out = self.run("aimem", "health")
        assert "backup_status=OK_VERIFIED" in out

    def t_doctor_corruption(self):
        p = self.root / "projects" / "alpha"
        ev = p / "EVENTS.jsonl"
        orig = ev.read_text()
        ev.write_text(orig + "{not json\n")
        rc, out = self.run("aimem", "doctor")
        assert rc == 1 and "invalid JSONL EVENTS.jsonl" in out
        ev.write_text(orig)
        man = json.loads((p / "project.json").read_text())
        good_cp = man["current_checkpoint"]
        man["current_checkpoint"] = "does-not-exist"
        (p / "project.json").write_text(json.dumps(man))
        rc, out = self.run("aimem", "doctor")
        assert rc == 1 and "current checkpoint metadata missing" in out
        man["current_checkpoint"] = good_cp
        (p / "project.json").write_text(json.dumps(man))
        cpc = p / "checkpoints" / good_cp / "CURRENT.md"
        saved = cpc.read_text()
        cpc.write_text(saved + "\ntampered\n")
        rc, out = self.run("aimem", "doctor", "--deep")
        assert rc == 1 and "hash mismatch" in out
        cpc.write_text(saved)
        (p / "CURRENT.md").write_text("# GENERATED AI CONTEXT PACK — NOT CANONICAL\n" + (p / "CURRENT.md").read_text())
        rc, out = self.run("aimem", "doctor")
        assert rc == 1 and "contamination" in out
        (p / "CURRENT.md").write_text((p / "CURRENT.md").read_text().split("\n", 1)[1])
        db = self.root / "registry" / "memory.db"
        for suf in ("", "-wal", "-shm"):
            q = Path(str(db) + suf)
            if q.exists():
                q.unlink()
        db.write_bytes(b"garbage" * 100)
        rc, out = self.run("aimem", "doctor")
        assert rc == 1 and "memory.db" in out
        rc, out = self.run("aimem", "doctor", "--repair", rc=0)
        assert "memory.db rebuilt" in out
        self.run("aimem", "doctor", "--deep", rc=0)

    def t_dashboard(self):
        port = _free_port()
        rc, out = self.run("aimem", "dashboard", "--port", str(port), rc=0)
        assert f"http://127.0.0.1:{port}/" in out
        try:
            html = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=20).read().decode()
            assert "AI Memory OS" in html and "alpha" in html
            js = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=20).read().decode())
            assert js["health"]["projects"] == 2 and any(p["slug"] == "alpha" for p in js["projects"])
            assert all("password" not in json.dumps(p).lower() or "REDACTED" in json.dumps(p) for p in js["projects"])
            rc, out = self.run("aimem", "dashboard", "--status", rc=0)
            assert "DASHBOARD_RUNNING=True" in out
            pid = int(out.split("pid=")[1].strip())
            try:
                lo = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True, timeout=30).stdout
                listens = [l for l in lo.splitlines() if "LISTEN" in l]
                assert listens and all("127.0.0.1:" in l for l in listens), f"dashboard must bind 127.0.0.1 only: {lo}"
            except FileNotFoundError:
                pass
            # non-loopback bind check: connecting through the LAN address must fail
            lan = _lan_ip()
            if lan:
                s = socket.socket()
                s.settimeout(1.5)
                refused = False
                try:
                    s.connect((lan, port))
                except OSError:
                    refused = True
                finally:
                    s.close()
                assert refused, "dashboard reachable on non-loopback address"
        finally:
            rc, out = self.run("aimem", "dashboard", "--stop", rc=0)
            assert "DASHBOARD_STOPPED" in out
        rc, out = self.run("aimem", "dashboard", "--status")
        assert rc == 1

    def t_migrate_idempotent(self):
        rc, out = self.run("aimem", "migrate", "--dry-run", rc=0)
        rc, out = self.run("aimem", "migrate", rc=0)
        rc, out2 = self.run("aimem", "migrate", "--dry-run", rc=0)
        assert "project.json +=" not in out2, "second migration must be a no-op for manifests"

    # ------------------------------------------------------------ full-mode tests
    def t_stress(self):
        rc = stress(self, sessions=10, steps=20)
        assert rc == 0

    def t_crash_recovery_end_to_end(self):
        """Agent begins, works, repo changes, process dies (no finish). Next agent must see RECOVERY_REQUIRED, get a packet, close, continue."""
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "claude", "--task", "big refactor", rc=0)
        sid = self.val(out, "SESSION_ID")
        self.run("aimem-step", "--project", "alpha", "--session", sid, "--kind", "write", "--summary", "refactored core", "--file", "core.py", "--result", "PASS", rc=0)
        self.commit(self.repo, "core.py", "refactored\n")
        (self.repo / "wip.py").write_text("half done")
        sf = self.root / "projects" / "alpha" / "sessions" / f"{sid}.json"
        j = json.loads(sf.read_text())
        j["lease"]["last_heartbeat"] = "2020-01-01T00:00:00+00:00"
        sf.write_text(json.dumps(j))
        rc, out = self.run("aimem", "begin", "alpha", "--agent", "codex", "--task", "continue", rc=0)
        sid2 = self.val(out, "SESSION_ID")
        assert "DIRTY_REPO" in out and "WARN=OTHER_OPEN_SESSION_EXISTS" in out
        ctx = Path(self.val(out, "CONTEXT")).read_text()
        assert "ABANDONED" in ctx and sid in ctx
        rc, out = self.run("aimem", "recover", "alpha")
        assert rc == 4 and sid in out
        rc, out = self.run("aimem", "recover", "--session", sid)
        assert "wip.py" in out and "core.py" in out and "refactored core" in out
        self.run("aimem", "session", "close", "alpha", "--session", sid, "--result", "ABANDONED", rc=0)
        (self.repo / "wip.py").unlink()
        self.set_current("alpha", "# Alpha\nSTATUS: RECOVERED_AND_CONTINUED\n")
        self.run("aimem", "finish", "alpha", "--session", sid2, "--result", "PASS", "--label", "recovered", rc=0)
        rc, out = self.run("aimem", "health")
        assert "recovery_required=0" in out

    # ------------------------------------------------------------ runner
    def run_all(self):
        tests = [
            ("init", self.t_init), ("project_registration", self.t_register), ("detection", self.t_detect), ("begin+bounded_context", self.t_begin),
            ("explicit_step_binding+redaction", self.t_step_binding), ("heartbeat", self.t_heartbeat), ("finish_unchanged_refused", self.t_finish_unchanged_refused),
            ("finish+checkpoint+provenance", self.t_finish), ("no_advance_current", self.t_checkpoint_no_advance), ("search+reindex+ranking", self.t_search_reindex),
            ("write_current_cas+secret_reject", self.t_write_current_cas), ("transaction_interruption", self.t_txn_interruption), ("locking_fail_closed", self.t_locking),
            ("two_concurrent_sessions+ambiguous_fail_close+cas", self.t_two_sessions_cas), ("stale_memory_detection", self.t_stale_memory),
            ("abandoned_session+recovery_packet", self.t_abandoned_recovery), ("compaction", self.t_compaction), ("artifact_dedup+object_verify", self.t_artifact_dedup),
            ("chat_export", self.t_chat_export), ("chat_import_dry_run+conflicts", self.t_chat_import), ("provenance", self.t_provenance), ("health", self.t_health),
            ("backup+isolated_restore_verification+restore", self.t_backup_restore), ("doctor_corruption_detection", self.t_doctor_corruption),
            ("dashboard_local_only", self.t_dashboard), ("migrate_idempotent", self.t_migrate_idempotent),
        ]
        if self.full:
            tests += [("stress_multi_agent", self.t_stress), ("crash_recovery_end_to_end", self.t_crash_recovery_end_to_end)]
        for name, fn in tests:
            self.check(name, fn)
        failed = [r for r in self.results if r[1] == "FAIL"]
        print(f"SELFTEST={'PASS' if not failed else 'FAIL'} passed={len(self.results) - len(failed)} failed={len(failed)} root={self.root}")
        report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "full": self.full, "results": [{"name": n, "result": r, "error": e, "seconds": round(s, 2)} for n, r, e, s in self.results]}
        (self.tmp / "SELFTEST_REPORT.json").write_text(json.dumps(report, indent=2))
        print(f"REPORT={self.tmp / 'SELFTEST_REPORT.json'}")
        return 0 if not failed else 1

    def cleanup(self):
        try:
            self.run("aimem", "dashboard", "--stop")
        except Exception:
            pass
        if not self.keep:
            shutil.rmtree(self.tmp, ignore_errors=True)
        else:
            print(f"KEPT={self.tmp}")


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return None if ip.startswith("127.") else ip
    except Exception:
        return None


# ---------------------------------------------------------------- stress

def stress(t: T, sessions=10, steps=20):
    """10 concurrent sessions × N steps on one project + independent projects; verify no cross-logging/corruption."""
    root = t.root
    t.make_repo(t.repo2, 2) if not (t.repo2 / ".git").exists() else None
    t.run("aimem", "register", "gamma", "--name", "Gamma", "--repo", str(t.repo2), rc=0)
    p = root / "projects" / "alpha"
    before_lines = sum(1 for f in (p / "cold" / "step-journal").glob("*.jsonl") for _ in f.open())
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(t.run, "aimem", "begin", slug, "--agent", "claude" if i % 2 else "codex", "--task", f"stress {i}", rc=0)
                for i, slug in enumerate(["alpha"] * sessions + ["beta", "gamma"] * 2)]
        outs = [f.result()[1] for f in futs]
    sids = [t.val(o, "SESSION_ID") for o in outs]
    alpha_sids = sids[:sessions]
    other = list(zip(["beta", "gamma"] * 2, sids[sessions:]))
    assert all(sids), "every begin must return an id"
    assert len(set(sids)) == len(sids), "session ids must be unique"
    jobs = []
    for sid in alpha_sids:
        for k in range(steps):
            jobs.append(("alpha", sid, k))
    for slug, sid in other:
        for k in range(steps):
            jobs.append((slug, sid, k))
    def step(job):
        slug, sid, k = job
        return t.run("aimem-step", "--project", slug, "--session", sid, "--kind", "write" if k % 3 else "test", "--summary", f"step {k} of {sid}",
                     "--result", "PASS" if k % 5 else "FAIL", "--file", f"f{k}.py", rc=0)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        list(ex.map(step, jobs))
    # verify attribution
    for sid in alpha_sids:
        lines = (p / "sessions" / f"{sid}.steps.jsonl").read_text().splitlines()
        assert len(lines) == steps, f"{sid}: {len(lines)} steps (expected {steps})"
        for l in lines:
            j = json.loads(l)
            assert j["session_id"] == sid and j["summary"].endswith(sid), "cross-session attribution"
    for slug, sid in other:
        lines = (root / "projects" / slug / "sessions" / f"{sid}.steps.jsonl").read_text().splitlines()
        assert len(lines) == steps
        assert all(json.loads(l)["session_id"] == sid for l in lines)
    after_lines = sum(1 for f in (p / "cold" / "step-journal").glob("*.jsonl") for _ in f.open())
    assert after_lines - before_lines == sessions * steps, f"daily journal lines {after_lines - before_lines} != {sessions * steps}"
    for f in (p / "cold" / "step-journal").glob("*.jsonl"):
        for l in f.open():
            json.loads(l)
    for l in (p / "EVENTS.jsonl").open():
        json.loads(l)
    # concurrent canonical finishes: exactly one per version; conflicts fail closed
    for i, sid in enumerate(alpha_sids):
        pass
    def fin(sid):
        (p / "CURRENT.md").write_text(f"# Alpha\nSTATUS: STRESS_{sid}\n")
        return t.run("aimem", "finish", "alpha", "--session", sid, "--result", "PASS", "--label", f"stress-{sid[-6:]}")
    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(fin, alpha_sids))
    rcs = [r[0] for r in res]
    ok = rcs.count(0)
    conflicts = rcs.count(3)
    assert ok >= 1, f"at least one finish must succeed: {rcs}"
    assert ok + conflicts + rcs.count(5) == len(rcs), f"unexpected rc set: {rcs} {[r[1][-300:] for r in res if r[0] not in (0, 3, 5)]}"
    # retry conflicted ones with explicit expect-version, sequentially
    for sid, (rc, out) in zip(alpha_sids, res):
        if rc != 0:
            man = json.loads((p / "project.json").read_text())
            t.run("aimem", "finish", "alpha", "--session", sid, "--result", "PASS", "--label", "stress-retry", "--allow-unchanged", "--expect-version", str(man["memory_version"]), rc=0)
    other_conflicts = 0
    for slug, sid in other:
        rc, out = t.run("aimem", "finish", slug, "--session", sid, "--result", "PASS", "--label", "stress-other", "--allow-unchanged")
        if rc == 3:  # a sibling session on the same project advanced memory first: expected CAS rejection, retry explicitly
            other_conflicts += 1
            man = json.loads((root / "projects" / slug / "project.json").read_text())
            t.run("aimem", "finish", slug, "--session", sid, "--result", "PASS", "--label", "stress-other-retry", "--allow-unchanged", "--expect-version", str(man["memory_version"]), rc=0)
        else:
            assert rc == 0, out[-800:]
    # integrity
    cps = list((p / "checkpoints").glob("*/meta.json"))
    for m in cps:
        j = json.loads(m.read_text())
        assert j["id"] == m.parent.name
    man = json.loads((p / "project.json").read_text())
    assert man["current_checkpoint"] and (p / "checkpoints" / man["current_checkpoint"] / "meta.json").exists()
    rc, out = t.run("aimem", "doctor", "--deep", rc=0)
    rc, out = t.run("aimem", "sessions", "alpha", "--open", rc=0)
    assert not [l for l in out.splitlines() if "OPEN/" in l], "all stress sessions must be closed"
    print(f"STRESS=PASS sessions={sessions} steps_per_session={steps} projects=3 finish_ok={ok} finish_conflicts={conflicts} other_project_conflicts={other_conflicts} lock_timeouts={rcs.count(5)} checkpoints={len(cps)}")
    return 0


def run(full=False, keep=False, verbose=False):
    t = T(full=full, keep=keep, verbose=verbose)
    try:
        return t.run_all()
    finally:
        t.cleanup()


def run_stress_only(sessions=10, steps=20, keep=False):
    t = T(full=True, keep=keep)
    try:
        t.t_init()
        t.t_register()
        rc = stress(t, sessions=sessions, steps=steps)
        print("STRESS_ONLY=PASS" if rc == 0 else "STRESS_ONLY=FAIL")
        return rc
    except AssertionError as e:
        print(f"STRESS_ONLY=FAIL {e}")
        return 1
    finally:
        t.cleanup()
