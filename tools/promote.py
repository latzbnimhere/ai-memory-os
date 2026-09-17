#!/usr/bin/env python3
"""Controlled promotion to AI Memory OS 4.1.0 with rehearsal, gates, verification and automatic rollback.

  python3 tools/promote.py --rehearse --root /path/to/AI-Memory
  python3 tools/promote.py --live --root /path/to/AI-Memory --confirm-live-root /path/to/AI-Memory --backup-dir D

Gates (all must pass before any live mutation): zero OPEN sessions on the target root (checked at preflight, before
the backup and again immediately before the first mutation), unit tests on an isolated root, full isolated selftest,
baseline doctor --deep on the target root, fresh verified backup of the target root. After install: migrate,
doctor --deep, health, hash verification, functional smoke (begin/step/finish on the ai-memory slug). Any failure after
the first mutation -> rollback to the preserved pre-4.1.0 files; a failure before it leaves the root untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
BIN_FILES = ["aimem", "aimem-detect", "aimem-step", "aimem-sweep"]
SHARE_FILES = ["AI_MEMORY_AGENT_PROTOCOL_V4.md", "AI_MEMORY_AUTOPILOT.md", "AI_MEMORY_CONTINUOUS_JOURNAL.md",
               "OTHER_AI_AGENT_INSTRUCTIONS.md", "SESSION_BINDING_V3_1.md", "AI_PROTOCOL.md"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def run(cmd, env=None, timeout=900, check=False):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(str(c) for c in cmd)} rc={r.returncode}\n{(r.stdout + r.stderr)[-3000:]}")
    return r.returncode, r.stdout + r.stderr


class Promotion:
    def __init__(self, root: Path, live: bool, backup_dir: Path, skip_selftest=False):
        self.root = root
        self.live = live
        self.backup_dir = backup_dir
        self.skip_selftest = skip_selftest
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(root))
        self.report = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "root": str(root), "live": live, "gates": {}, "steps": [], "result": "NOT_RUN"}
        self.preserved = None

    def gate(self, name, ok, detail=""):
        self.report["gates"][name] = {"result": "PASS" if ok else "FAIL", "detail": detail[-1500:]}
        print(f"GATE {name}: {'PASS' if ok else 'FAIL'}")
        if not ok:
            raise RuntimeError(f"gate failed: {name}\n{detail[-2000:]}")

    def step(self, name, detail=""):
        self.report["steps"].append({"name": name, "detail": detail[-1500:]})
        print(f"STEP {name}")

    def zero_open_sessions_gate(self, name):
        """Fail closed unless the installed aimem on the target root reports no OPEN session.

        Never closes, supersedes or re-attributes sessions: any open session (or any failure to
        list them) blocks promotion and must be settled by its owner.
        """
        rc, out = run([sys.executable, str(self.root / "bin" / "aimem"), "sessions", "--open"], env=self.env, timeout=120)
        self.gate(name, rc == 0 and out.strip() == "", f"rc={rc}\n{out}")

    # ------------------------------------------------------------ gates
    def pre_gates(self):
        self.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_PREFLIGHT")
        # unit tests get their own empty memory root so no test can ever resolve to the target root
        unit_root = Path(tempfile.mkdtemp(prefix="aimem-promote-unit-root-"))
        test_env = dict(os.environ, PYTHONPATH=str(DEV / "lib"), AI_MEMORY_ROOT=str(unit_root))
        rc, out = run(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                str(DEV / "tests"),
                "-v",
            ],
            env=test_env,
            timeout=600,
        )
        shutil.rmtree(unit_root, ignore_errors=True)
        self.gate(
            "FULL_UNIT_TESTS",
            rc == 0
            and "OK" in out
            and "V41_TARGETED_REGRESSION=PASS" in out,
            out,
        )
        if not self.skip_selftest:
            rc, out = run([sys.executable, str(DEV / "bin" / "aimem"), "selftest", "--full"], env=dict(os.environ), timeout=1200)
            self.gate("FULL_SELFTEST", rc == 0 and "SELFTEST=PASS" in out, out)
        base_bin = self.root / "bin" / "aimem"
        rc, out = run([sys.executable, str(base_bin), "doctor", "--deep"], env=self.env)
        self.gate("BASELINE_DOCTOR_DEEP", rc == 0, out)
        rc, out = run([sys.executable, str(base_bin), "version"], env=self.env)
        self.report["baseline_version"] = out.strip().splitlines()[0] if out else "?"
        self.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_BEFORE_BACKUP")
        # fresh verified backup with the V4 backup module (manifest + isolated restore verify)
        rc, out = run([sys.executable, str(DEV / "bin" / "aimem"), "backup", "--output-dir", str(self.backup_dir), "--label", "pre-4.1.0-promotion", "--verify"], env=self.env)
        self.gate("PRE_PROMOTION_BACKUP_VERIFIED", rc == 0 and "BACKUP_VERIFY=PASS" in out, out)
        self.report["pre_promotion_backup"] = next((l.split()[-1] for l in out.splitlines() if l.startswith("BACKUP=PASS")), None)

    # ------------------------------------------------------------ install
    def preserve(self):
        stamp = time.strftime("%Y%m%dT%H%M%S%z")
        keep = self.root / ".previous" / f"pre-4.1.0-{stamp}"
        (keep / "bin").mkdir(parents=True)
        for f in BIN_FILES:
            src = self.root / "bin" / f
            if src.exists():
                shutil.copy2(src, keep / "bin" / f)
        for f in SHARE_FILES + ["PATCH_LEVEL", "config.json", "VERSION"]:
            src = self.root / f
            if src.exists():
                shutil.copy2(src, keep / f)

        live_lib = self.root / "lib"
        if live_lib.exists():
            shutil.copytree(
                live_lib,
                keep / "lib",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

        live_docs = self.root / "docs" / "v4"
        if live_docs.exists():
            shutil.copytree(
                live_docs,
                keep / "docs-v4",
            )

        self.preserved = keep
        self.step("PRESERVE_PRE_4_1_0_FILES", str(keep))

    def install(self):
        for f in BIN_FILES:
            shutil.copy2(DEV / "bin" / f, self.root / "bin" / f)
            os.chmod(self.root / "bin" / f, 0o755)
        lib_dst = self.root / "lib"
        if lib_dst.exists():
            shutil.rmtree(lib_dst)
        shutil.copytree(DEV / "lib", lib_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for f in SHARE_FILES:
            src = DEV / "share" / f
            if src.exists():
                shutil.copy2(src, self.root / f)
        docs_dst = self.root / "docs" / "v4"
        if docs_dst.exists():
            shutil.rmtree(docs_dst)
        shutil.copytree(DEV / "docs", docs_dst)
        shutil.copy2(DEV / "README.md", self.root / "docs" / "v4" / "README.md")
        (self.root / "VERSION").write_text("4.1.0\n")
        self.step("INSTALL_BIN_LIB_DOCS")
        # hash verification
        mism = []
        for f in BIN_FILES:
            if sha(DEV / "bin" / f) != sha(self.root / "bin" / f):
                mism.append(f)
        for f in (DEV / "lib" / "aimem").glob("*.py"):
            if sha(f) != sha(self.root / "lib" / "aimem" / f.name):
                mism.append(f.name)
        self.gate("INSTALLED_HASHES_MATCH_DEV", not mism, str(mism))

    def migrate_and_verify(self):
        v4 = self.root / "bin" / "aimem"
        rc, out = run([sys.executable, str(v4), "version"], env=self.env)
        self.gate("V4_EXECUTABLE_RUNS", rc == 0 and "aimem 4.1." in out, out)
        rc, out = run([sys.executable, str(v4), "migrate", "--dry-run"], env=self.env)
        self.step("MIGRATE_DRY_RUN", out)
        rc, out = run([sys.executable, str(v4), "migrate"], env=self.env)
        self.gate("MIGRATE", rc == 0 and "MIGRATE=PASS" in out, out)
        rc, out = run([sys.executable, str(v4), "doctor", "--deep"], env=self.env)
        self.gate("POST_MIGRATION_DOCTOR_DEEP", rc == 0, out)
        self.report["post_doctor"] = out[-2000:]
        rc, out = run([sys.executable, str(v4), "health", "--verbose"], env=self.env)
        self.gate("HEALTH", rc in (0, 1), out)
        self.report["health"] = out[-3000:]
        rc, out = run([sys.executable, str(v4), "status"], env=self.env)
        self.report["status"] = out
        self.gate("STATUS", rc == 0, out)

    def smoke(self):
        v4 = self.root / "bin" / "aimem"
        step = self.root / "bin" / "aimem-step"
        rc, out = run([sys.executable, str(v4), "begin", "ai-memory", "--agent", "other", "--task", "promotion smoke test", "--tokens", "2000"], env=self.env)
        self.gate("SMOKE_BEGIN", rc == 0 and "SESSION_ID=" in out, out)
        sid = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("SESSION_ID="))
        rc, out = run([sys.executable, str(step), "--project", "ai-memory", "--session", sid, "--kind", "test", "--summary", "promotion smoke step", "--result", "PASS"], env=self.env)
        self.gate("SMOKE_STEP", rc == 0 and "binding=explicit" in out, out)
        rc, out = run([sys.executable, str(v4), "finish", "ai-memory", "--session", sid, "--result", "PASS", "--label", "v4-promotion-smoke", "--allow-unchanged", "--no-advance-current"], env=self.env)
        self.gate("SMOKE_FINISH_ADMIN", rc == 0 and "FINISH=PASS" in out, out)
        rc, out = run([sys.executable, str(v4), "search", "ai-memory", "V4"], env=self.env)
        self.gate("SMOKE_SEARCH", rc == 0, out)
        rc, out = run([sys.executable, str(v4), "recover"], env=self.env)
        self.report["recover_scan"] = out[-2000:]
        self.step("RECOVER_SCAN", out)

    def integrate_live(self):
        v4 = self.root / "bin" / "aimem"
        rc, out = run([sys.executable, str(v4), "integrate-global", "--agent", "all"], env=self.env)
        self.gate("GLOBAL_INSTRUCTIONS", rc == 0 and out.count("verified=True") == 2, out)
        self.report["integration"] = out
        # LaunchAgent: same plist path/args; restart so it runs the V4 launcher
        uid = os.getuid()
        rc, out = run(["launchctl", "kickstart", "-k", f"gui/{uid}/io.aimemory.sweep"])
        time.sleep(3)
        rc2, out2 = run([sys.executable, str(self.root / "bin" / "aimem-sweep")], env=self.env)
        self.gate("LAUNCHAGENT_SWEEP_RUNS_V4", rc2 == 0 and "SWEEP=PASS" in out2, out + out2)
        rc, out = run(["launchctl", "list"])
        self.gate("LAUNCHAGENT_LOADED", "io.aimemory.sweep" in out, out[-500:])

    # ------------------------------------------------------------ rollback
    def rollback(self, reason):
        print(f"ROLLBACK: {reason}")
        actions = []
        if self.preserved is None:
            # failed before preserve(): the target root was never mutated, so there is nothing to undo
            # (moving lib/docs aside here would break a healthy installation)
            self.report["rollback"] = {"reason": reason, "actions": ["NO_MUTATION_BEFORE_FAILURE"]}
            print("ROLLBACK_RESULT=NOT_REQUIRED_NO_MUTATION")
            return
        try:
            if self.preserved:
                for f in BIN_FILES:
                    src = self.preserved / "bin" / f
                    if src.exists():
                        shutil.copy2(src, self.root / "bin" / f)
                        actions.append(f"restored bin/{f}")
                for f in SHARE_FILES + ["PATCH_LEVEL", "config.json"]:
                    src = self.preserved / f
                    if src.exists():
                        shutil.copy2(src, self.root / f)
                        actions.append(f"restored {f}")
                if (self.preserved / "VERSION").exists():
                    shutil.copy2(self.preserved / "VERSION", self.root / "VERSION")
                elif (self.root / "VERSION").exists():
                    (self.root / "VERSION").unlink()
                    actions.append("removed VERSION")
            live_lib = self.root / "lib"

            if live_lib.exists():
                failed_lib = (
                    self.root
                    / ".previous"
                    / f"lib-v4-failed-{int(time.time())}"
                )
                shutil.move(str(live_lib), str(failed_lib))
                actions.append(f"moved failed lib aside -> {failed_lib}")

            preserved_lib = self.preserved / "lib" if self.preserved else None
            if preserved_lib and preserved_lib.exists():
                shutil.copytree(preserved_lib, live_lib)
                actions.append("restored preserved lib")

            live_docs = self.root / "docs" / "v4"
            if live_docs.exists():
                failed_docs = (
                    self.root
                    / ".previous"
                    / f"docs-v4-failed-{int(time.time())}"
                )
                failed_docs.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(live_docs), str(failed_docs))
                actions.append(f"moved failed docs aside -> {failed_docs}")

            preserved_docs = (
                self.preserved / "docs-v4"
                if self.preserved
                else None
            )
            if preserved_docs and preserved_docs.exists():
                live_docs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(preserved_docs, live_docs)
                actions.append("restored preserved docs/v4")
            if self.live:
                bk = self.report.get("global_instruction_backups") or {}
                for path, b in bk.items():
                    if Path(b).exists():
                        shutil.copy2(b, path)
                        actions.append(f"restored {path}")
            rc, out = run([sys.executable, str(self.root / "bin" / "aimem"), "doctor", "--deep"], env=self.env)
            actions.append(f"baseline doctor rc={rc}")
            self.report["rollback"] = {"reason": reason, "actions": actions, "baseline_doctor_rc": rc}
            print("ROLLBACK_RESULT=" + ("PASS" if rc == 0 else "DOCTOR_FAIL_AFTER_ROLLBACK"))
        except Exception as e:  # noqa: BLE001
            self.report["rollback"] = {"reason": reason, "actions": actions, "error": str(e)}
            print(f"ROLLBACK_RESULT=ERROR {e}")

    # ------------------------------------------------------------ run
    def run(self):
        try:
            self.pre_gates()
            self.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_BEFORE_FIRST_MUTATION")
            if self.live:
                self.report["global_instruction_backups"] = {}
                for p in (Path.home() / ".claude" / "CLAUDE.md", Path.home() / ".codex" / "AGENTS.md"):
                    if p.exists():
                        b = self.backup_dir / f"{p.name}.pre-v4-{int(time.time())}.bak"
                        shutil.copy2(p, b)
                        self.report["global_instruction_backups"][str(p)] = str(b)
            self.preserve()
            self.install()
            self.migrate_and_verify()
            self.smoke()
            if self.live:
                self.integrate_live()
            self.report["result"] = "PASS"
        except Exception as e:  # noqa: BLE001
            self.report["result"] = "FAIL"
            self.report["error"] = str(e)[-3000:]
            self.rollback(str(e)[:300])
        finally:
            out = self.root / "logs" / f"PROMOTION_{'LIVE' if self.live else 'REHEARSAL'}_{time.strftime('%Y%m%dT%H%M%S%z')}.json"
            out.parent.mkdir(exist_ok=True)
            out.write_text(json.dumps(self.report, indent=2))
            dev_copy = DEV / "reports"
            dev_copy.mkdir(exist_ok=True)
            shutil.copy2(out, dev_copy / out.name)
            print(f"PROMOTION_RESULT={self.report['result']} report={out}")
        return 0 if self.report["result"] == "PASS" else 1


def rehearse(args, source_root):
    tmp = Path(tempfile.mkdtemp(prefix="aimem-rehearsal-"))
    copy = tmp / "AI-Memory"
    print(f"REHEARSAL_COPY={copy}")
    print(f"REHEARSAL_SOURCE={source_root}")
    shutil.copytree(source_root, copy, ignore=shutil.ignore_patterns(".locks", ".run"), symlinks=True)
    # snapshot canonical hashes for comparison
    def canon(root):
        d = {}
        for p in sorted((root / "projects").glob("*/")):
            slug = p.name
            d[slug] = {"CURRENT": sha(p / "CURRENT.md") if (p / "CURRENT.md").exists() else None,
                       "NEXT": sha(p / "NEXT.md") if (p / "NEXT.md").exists() else None,
                       "checkpoints": sorted(x.name for x in (p / "checkpoints").glob("*")) if (p / "checkpoints").exists() else [],
                       "current_checkpoint": json.loads((p / "project.json").read_text()).get("current_checkpoint"),
                       "events_lines": sum(1 for _ in open(p / "EVENTS.jsonl")) if (p / "EVENTS.jsonl").exists() else 0}
        return d
    before = canon(copy)
    pr = Promotion(copy, live=False, backup_dir=tmp / "backups", skip_selftest=args.skip_selftest)
    rc = pr.run()
    after = canon(copy)
    cmp = {"canonical_preserved": True, "detail": []}
    for slug in before:
        b, a = before[slug], after.get(slug, {})
        if b["CURRENT"] != a.get("CURRENT") or b["NEXT"] != a.get("NEXT"):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: CURRENT/NEXT changed")
        if slug != "ai-memory" and b["current_checkpoint"] != a.get("current_checkpoint"):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: current checkpoint changed")
        if not set(b["checkpoints"]).issubset(set(a.get("checkpoints", []))):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: checkpoint lost")
        if a.get("events_lines", 0) < b["events_lines"]:
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: EVENTS shrank")
    print(f"REHEARSAL_CANONICAL_PRESERVED={cmp['canonical_preserved']} {cmp['detail'] or ''}")
    live_before = canon(source_root)
    # live root must be byte-identical in canonical files to the pre-rehearsal copy (sweep may append EVENTS/AUTO state only)
    live_ok = all(live_before[s]["CURRENT"] == before[s]["CURRENT"] and live_before[s]["checkpoints"] == before[s]["checkpoints"] for s in before)
    print(f"LIVE_ROOT_UNTOUCHED_BY_REHEARSAL={live_ok}")
    rep = {"rehearsal_copy": str(copy), "promotion_result": pr.report["result"], "canonical_comparison": cmp, "live_untouched": live_ok, "gates": pr.report["gates"]}
    (DEV / "reports").mkdir(exist_ok=True)
    (DEV / "reports" / f"MIGRATION_REHEARSAL_{time.strftime('%Y%m%dT%H%M%S%z')}.json").write_text(json.dumps(rep, indent=2))
    ok = rc == 0 and cmp["canonical_preserved"] and live_ok
    print(f"MIGRATION_REHEARSAL={'PASS' if ok else 'FAIL'}")
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"KEPT={tmp}")
    return 0 if ok else 1


def _resolve_root(value):
    return Path(value).expanduser().resolve()


def main():
    ap = argparse.ArgumentParser()

    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rehearse", action="store_true")
    mode.add_argument("--live", action="store_true")

    ap.add_argument(
        "--root",
        required=True,
        help="Explicit existing AI Memory installation root. No implicit live root is permitted.",
    )
    ap.add_argument(
        "--confirm-live-root",
        help="Required with --live; must resolve to exactly the same path as --root.",
    )
    ap.add_argument(
        "--backup-dir",
        help="Backup directory. Defaults to a sibling <root-name>-Backups directory.",
    )
    ap.add_argument(
        "--skip-selftest",
        action="store_true",
        help="reuse an already-passed selftest (rehearsal only)",
    )
    ap.add_argument("--keep", action="store_true")

    a = ap.parse_args()

    root = _resolve_root(a.root)

    if not root.exists() or not root.is_dir():
        ap.error("--root must be an existing directory")

    if root == Path("/").resolve():
        ap.error("--root may not be filesystem root")

    if root == Path.home().resolve():
        ap.error("--root may not be the user's home directory")

    if not (root / "bin" / "aimem").is_file():
        ap.error("--root does not look like an AI Memory installation (bin/aimem missing)")

    if a.rehearse:
        raise SystemExit(rehearse(a, root))

    if not a.confirm_live_root:
        ap.error(
            "--live requires --confirm-live-root with the exact same explicit target path"
        )

    confirmed = _resolve_root(a.confirm_live_root)

    if confirmed != root:
        ap.error("--confirm-live-root does not match --root")

    backup_dir = (
        _resolve_root(a.backup_dir)
        if a.backup_dir
        else root.parent / f"{root.name}-Backups"
    )

    pr = Promotion(
        root,
        live=True,
        backup_dir=backup_dir,
        skip_selftest=a.skip_selftest,
    )

    raise SystemExit(pr.run())


if __name__ == "__main__":
    main()
