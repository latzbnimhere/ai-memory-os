#!/usr/bin/env python3
"""Controlled promotion to the AI Memory OS version in ./VERSION with rehearsal, gates, verification and automatic rollback.

  python3 tools/promote.py --rehearse --root /path/to/AI-Memory
  python3 tools/promote.py --live --root /path/to/AI-Memory --confirm-live-root /path/to/AI-Memory --backup-dir D

Gates (all must pass before any live mutation): zero OPEN sessions on the target root (checked at preflight, before
the backup and again immediately before the first mutation), Drive layers preserved (a root using the Drive mirror or
the stable Drive-id handoff pin refuses a source without them), unit tests on an isolated root, full isolated selftest,
baseline doctor --deep on the target root, fresh verified backup of the target root. After install: migrate,
doctor --deep, health, hash verification, functional smoke (begin/step/finish on --smoke-slug, default ai-memory). Any failure after
the first mutation -> rollback to the preserved pre-promotion files; a failure before it leaves the root untouched.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

DEV = Path(__file__).resolve().parents[1]
DEV_VERSION = (DEV / "VERSION").read_text().strip()
BIN_FILES = ["aimem", "aimem-detect", "aimem-step", "aimem-sweep"]
SHARE_FILES = ["AI_MEMORY_AGENT_PROTOCOL_V4.md", "AI_MEMORY_AUTOPILOT.md", "AI_MEMORY_CONTINUOUS_JOURNAL.md",
               "OTHER_AI_AGENT_INSTRUCTIONS.md", "SESSION_BINDING_V3_1.md", "AI_PROTOCOL.md"]


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _launchagent_module():
    """The NEW engine's lib/aimem/launchagent.py, loaded by path: importing the aimem package would bind
    AI_MEMORY_ROOT at import time."""
    spec = importlib.util.spec_from_file_location("aimem_launchagent", DEV / "lib" / "aimem" / "launchagent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(cmd, env=None, timeout=900, check=False):
    r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(str(c) for c in cmd)} rc={r.returncode}\n{(r.stdout + r.stderr)[-3000:]}")
    return r.returncode, r.stdout + r.stderr


class Promotion:
    def __init__(self, root: Path, live: bool, backup_dir: Path, skip_selftest=False, smoke_slug="ai-memory", home=None):
        self.root = root
        self.smoke_slug = smoke_slug
        self.smoke_sid = None  # an OPEN smoke session that rollback must close
        self.live = live
        self.backup_dir = backup_dir
        self.skip_selftest = skip_selftest
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(root))
        if home is not None:  # rehearsal: never read or write the owner's real HOME (Drive, backups, LaunchAgents)
            Path(home).mkdir(parents=True, exist_ok=True)
            self.env["HOME"] = str(home)
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

    def smoke_slug_gate(self):
        """Fail before any mutation when the smoke project is not registered on the target root."""
        try:
            reg = json.loads((self.root / "registry" / "projects.json").read_text())
            ok = self.smoke_slug in reg.get("projects", {}) and (self.root / "projects" / self.smoke_slug / "project.json").is_file()
        except (OSError, ValueError):
            ok = False
        self.gate("SMOKE_SLUG_REGISTERED", ok, f"--smoke-slug {self.smoke_slug!r} must be a registered project on {self.root}")

    def migrate_precheck_gate(self):
        """The NEW engine's read-only migrate dry-run against the untouched target: refuses a root, config or
        manifest written by a newer engine and pending transactions. Must run before install() overwrites VERSION."""
        rc, out = run([sys.executable, str(DEV / "bin" / "aimem"), "migrate", "--dry-run"], env=self.env)
        self.gate("NEW_ENGINE_MIGRATE_PRECHECK", rc == 0 and "MIGRATE=DRY_RUN" in out, out)

    def drive_layer_gate(self):
        """install() replaces lib/ wholesale; that is how the 4.2.0 promotion silently reverted the stable Drive identity.
        A target that uses the Drive layers (verified mirror registry, or a handoff pin on the drivefs-item-id-v1
        scheme) is never promoted to a source that lacks them or reintroduces device-number identity."""
        aimem_src = DEV / "lib" / "aimem"
        uses_mirror = (self.root / "registry" / "drive-mirror.json").exists()
        scheme = None
        hreg = self.root / "registry" / "handoffs.json"
        if hreg.exists():
            try:
                scheme = (json.loads(hreg.read_text()).get("drive") or {}).get("identity_scheme") or "LEGACY"
            except (OSError, ValueError):
                scheme = "UNREADABLE"
        needed = (["drive.py", "driveid.py"] if uses_mirror else []) + (["driveid.py"] if scheme == "drivefs-item-id-v1" else [])
        missing = [n for n in dict.fromkeys(needed) if not (aimem_src / n).is_file()]
        regress = any(".st_dev" in (aimem_src / n).read_text(errors="ignore")
                      for n in ("handoff.py", "driveid.py", "drive.py") if (aimem_src / n).is_file())
        ok = not missing and scheme != "UNREADABLE" and not ((uses_mirror or scheme) and regress)
        self.gate("DRIVE_LAYER_PRESERVED", ok,
                  f"uses_mirror={uses_mirror} handoff_scheme={scheme} missing_in_source={missing} st_dev_identity={regress}")

    # ------------------------------------------------------------ gates
    def pre_gates(self):
        self.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_PREFLIGHT")
        self.smoke_slug_gate()
        self.drive_layer_gate()
        self.migrate_precheck_gate()
        # unit tests get their own empty memory root so no test can ever resolve to the target root
        unit_root = Path(tempfile.mkdtemp(prefix="aimem-promote-unit-root-"))
        test_env = dict(os.environ, PYTHONPATH=str(DEV / "lib"), AI_MEMORY_ROOT=str(unit_root), HOME=str(unit_root))
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
            st_home = Path(tempfile.mkdtemp(prefix="aimem-promote-selftest-home-"))
            rc, out = run([sys.executable, str(DEV / "bin" / "aimem"), "selftest", "--full"], env=dict(os.environ, HOME=str(st_home)), timeout=1200)
            shutil.rmtree(st_home, ignore_errors=True)
            self.gate("FULL_SELFTEST", rc == 0 and "SELFTEST=PASS" in out, out)
        base_bin = self.root / "bin" / "aimem"
        rc, out = run([sys.executable, str(base_bin), "doctor", "--deep"], env=self.env)
        self.gate("BASELINE_DOCTOR_DEEP", rc == 0, out)
        rc, out = run([sys.executable, str(base_bin), "version"], env=self.env)
        self.report["baseline_version"] = out.strip().splitlines()[0] if out else "?"
        self.zero_open_sessions_gate("ZERO_OPEN_SESSIONS_BEFORE_BACKUP")
        # fresh verified backup with the V4 backup module (manifest + isolated restore verify)
        rc, out = run([sys.executable, str(DEV / "bin" / "aimem"), "backup", "--output-dir", str(self.backup_dir), "--label", f"pre-{DEV_VERSION}-promotion", "--verify"], env=self.env)
        self.gate("PRE_PROMOTION_BACKUP_VERIFIED", rc == 0 and "BACKUP_VERIFY=PASS" in out, out)
        self.report["pre_promotion_backup"] = next((l.split()[-1] for l in out.splitlines() if l.startswith("BACKUP=PASS")), None)

    # ------------------------------------------------------------ install
    def preserve(self):
        stamp = time.strftime("%Y%m%dT%H%M%S%z")
        keep = self.root / ".previous" / f"pre-{DEV_VERSION}-{stamp}"
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
        self.step("PRESERVE_PRE_PROMOTION_FILES", str(keep))

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
        (self.root / "VERSION").write_text(DEV_VERSION + "\n")
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
        self.gate("V4_EXECUTABLE_RUNS", rc == 0 and f"aimem {DEV_VERSION}" in out, out)
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
        rc, out = run([sys.executable, str(v4), "begin", self.smoke_slug, "--agent", "other", "--task", "promotion smoke test", "--tokens", "2000"], env=self.env)
        self.gate("SMOKE_BEGIN", rc == 0 and "SESSION_ID=" in out, out)
        sid = next(l.split("=", 1)[1] for l in out.splitlines() if l.startswith("SESSION_ID="))
        self.smoke_sid = sid
        rc, out = run([sys.executable, str(step), "--project", self.smoke_slug, "--session", sid, "--kind", "test", "--summary", "promotion smoke step", "--result", "PASS"], env=self.env)
        self.gate("SMOKE_STEP", rc == 0 and "binding=explicit" in out, out)
        rc, out = run([sys.executable, str(v4), "finish", self.smoke_slug, "--session", sid, "--result", "PASS", "--label", "v4-promotion-smoke", "--allow-unchanged", "--no-advance-current"], env=self.env)
        self.gate("SMOKE_FINISH_ADMIN", rc == 0 and "FINISH=PASS" in out, out)
        self.smoke_sid = None
        rc, out = run([sys.executable, str(v4), "search", self.smoke_slug, "V4"], env=self.env)
        self.gate("SMOKE_SEARCH", rc == 0, out)
        rc, out = run([sys.executable, str(v4), "recover"], env=self.env)
        self.report["recover_scan"] = out[-2000:]
        self.step("RECOVER_SCAN", out)

    def integrate_live(self):
        v4 = self.root / "bin" / "aimem"
        rc, out = run([sys.executable, str(v4), "integrate-global", "--agent", "all"], env=self.env)
        self.gate("GLOBAL_INSTRUCTIONS", rc == 0 and out.count("verified=True") == 2, out)
        self.report["integration"] = out
        # LaunchAgent: same plist path/args; restart so it runs the V4 launcher. It is found by the aimem-sweep it
        # runs, not by name: installations older than the open-source release use their own label.
        la = _launchagent_module()
        label, plist = la.find(self.root)
        self.report["launchagent"] = {"label": label, "plist": str(plist)}
        uid = os.getuid()
        rc, out = run(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        time.sleep(3)
        rc2, out2 = run([sys.executable, str(self.root / "bin" / "aimem-sweep")], env=self.env)
        self.gate("LAUNCHAGENT_SWEEP_RUNS_V4", rc2 == 0 and "SWEEP=PASS" in out2, f"kickstart {label} rc={rc}\n" + out + out2)
        rc, out = run(["launchctl", "list"])
        self.gate("LAUNCHAGENT_LOADED", la.is_loaded(label, out), f"label={label} plist={plist}\n" + out[-500:])

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
            session_ok = True
            if self.smoke_sid:
                # the smoke session was opened by the new engine; an OPEN session would block every future
                # promotion (zero-open-sessions gate), so close it administratively with the restored engine
                rc_s, out_s = run([sys.executable, str(self.root / "bin" / "aimem"), "session", "close", self.smoke_slug,
                                   "--session", self.smoke_sid, "--result", "ABANDONED", "--note", "promotion rollback"],
                                  env=self.env)
                session_ok = rc_s == 0
                actions.append(f"closed smoke session {self.smoke_sid} rc={rc_s}")
            rc, out = run([sys.executable, str(self.root / "bin" / "aimem"), "doctor", "--deep"], env=self.env)
            actions.append(f"baseline doctor rc={rc}")
            self.report["rollback"] = {"reason": reason, "actions": actions, "baseline_doctor_rc": rc,
                                       "smoke_session_closed": session_ok}
            print("ROLLBACK_RESULT=" + ("PASS" if rc == 0 and session_ok else
                                        "SMOKE_SESSION_LEFT_OPEN" if not session_ok else "DOCTOR_FAIL_AFTER_ROLLBACK"))
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


def _within(path, base):
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(base))
        return True
    except ValueError:
        return False


def _isolate_copy(source_root, copy, tmp):
    """Make the rehearsal copy unable to reach anything live.

    - the optional Drive handoff bridge is disabled in the copy (a smoke finish would otherwise publish
      the throwaway copy's state to the owner's real handoff folder);
    - backups of the copy go under tmp, not the owner's backup directory;
    - symlinks: links resolving inside the live root are re-pointed into the copy; links resolving
      outside it are replaced by a detached copy of their content (writes through them would otherwise
      land in live data); dangling links are removed.
    """
    stats = {"handoff_bridge_disabled": False, "links_rewritten": 0, "links_dereferenced": 0, "links_removed": 0}
    hj = copy / "registry" / "handoffs.json"
    if hj.exists() or (copy / ".handoff").exists():
        stats["handoff_bridge_disabled"] = True
        if hj.exists():
            hj.unlink()
        shutil.rmtree(copy / ".handoff", ignore_errors=True)
    cfgp = copy / "config.json"
    cfg = json.loads(cfgp.read_text()) if cfgp.exists() else {}
    cfg["backup_dir"] = str(tmp / "backups")
    cfgp.write_text(json.dumps(cfg, indent=2) + "\n")
    src_real = os.path.realpath(source_root)
    for dirpath, dirnames, filenames in os.walk(copy):
        for name in list(dirnames) + list(filenames):
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                continue
            rel = os.path.relpath(p, copy)
            real = os.path.realpath(os.path.join(source_root, rel))  # where it points from the live root
            os.unlink(p)
            if _within(real, src_real):
                inside = os.path.join(str(copy), os.path.relpath(real, src_real))
                os.symlink(os.path.relpath(inside, os.path.dirname(p)), p)
                stats["links_rewritten"] += 1
            elif os.path.isdir(real):
                shutil.copytree(real, p, symlinks=False, ignore_dangling_symlinks=True)
                stats["links_dereferenced"] += 1
            elif os.path.isfile(real):
                shutil.copy2(real, p)
                stats["links_dereferenced"] += 1
            else:
                stats["links_removed"] += 1
    return stats


# Paths the launchd sweep may legitimately touch in the live root during a rehearsal.
_SWEEP_WRITES = ("AUTO_PHYSICAL_STATE.json",)


def _live_manifest(root):
    """lstat fingerprint of every entry of the live root (no hashing: cheap on large roots)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir.split(os.sep)[0] in (".locks", ".run"):
            dirnames[:] = []
            continue
        for name in dirnames + filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if rel.startswith(("registry" + os.sep + "memory.db",)):
                continue
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            out[rel] = (st.st_size, st.st_mtime_ns, st.st_ino, st.st_mode) if not os.path.isdir(os.path.join(dirpath, name)) \
                else ("dir", st.st_mode)
    return out


def _live_changes(before, after):
    changes = []
    for rel in sorted(set(before) | set(after)):
        b, a = before.get(rel), after.get(rel)
        if b == a:
            continue
        name = os.path.basename(rel)
        parts = rel.split(os.sep)
        if name in _SWEEP_WRITES or "physical-journal" in parts or name == ".DS_Store" \
                or any(name.startswith(f".{w}.") and name.endswith(".tmp") for w in _SWEEP_WRITES):
            continue  # launchd sweep writes (incl. its transient atomic-write temp files) and Finder metadata
        if name == "EVENTS.jsonl" and b and a and a[0] >= b[0]:
            continue  # append-only growth by the sweep
        if b and a and b[0] == "dir" and a[0] == "dir":
            continue  # directory mtime changes are implied by the checks on its entries
        changes.append(f"{rel}: {'added' if not b else 'removed' if not a else 'modified'}")
    return changes


# Runtime logs the launchd sweep appends to while a rehearsal runs (its StandardOutPath, opened O_APPEND each run).
# Such a log may only grow by appending: the same regular file (device, inode, mode) with every pre-rehearsal byte
# intact as its prefix. It is judged by content instead of the lstat manifest; every other path stays strict.
_APPEND_ONLY_LOGS = (os.path.join("logs", "aimem-sweep.out.log"),)


def _append_only_snapshot(root):
    """{rel: identity + prefix hash} of each append-only log present as a regular file (absent or not a regular
    file: no tolerance, the manifest comparison applies unchanged)."""
    snap = {}
    for rel in _APPEND_ONLY_LOGS:
        try:
            fd = os.open(os.path.join(root, rel), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            continue
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if not stat.S_ISREG(st.st_mode):
                continue
            data = fh.read(st.st_size)
        snap[rel] = {"dev": st.st_dev, "ino": st.st_ino, "mode": st.st_mode, "size": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
    return snap


def _append_only_verdict(root, rel, before):
    """(ok, detail) for one append-only log against its pre-rehearsal snapshot."""
    try:
        fd = os.open(os.path.join(root, rel), os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False, "removed"
    except OSError as e:
        return False, f"replaced or unreadable ({e.strerror})"
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode) or (st.st_dev, st.st_ino) != (before["dev"], before["ino"]):
            return False, "replaced (not the same file)"
        if st.st_mode != before["mode"]:
            return False, "mode changed"
        if st.st_size < before["size"]:
            return False, f"truncated ({before['size']} -> {st.st_size} bytes)"
        prefix = fh.read(before["size"])
    if len(prefix) != before["size"] or hashlib.sha256(prefix).hexdigest() != before["sha256"]:
        return False, "existing bytes modified"
    grown = st.st_size - before["size"]
    return True, f"appended {grown} bytes" if grown else "unchanged"


def _live_root_changes(root, manifest_before, logs_before):
    """(unexpected changes, expected concurrent writes) of the live root since the pre-rehearsal snapshots."""
    changes = _live_changes({k: v for k, v in manifest_before.items() if k not in logs_before},
                            {k: v for k, v in _live_manifest(root).items() if k not in logs_before})
    expected = []
    for rel, b in sorted(logs_before.items()):
        ok, detail = _append_only_verdict(root, rel, b)
        (expected if ok else changes).append(f"{rel}: {detail}")
    return sorted(changes), expected


def _remove_tree(path):
    """Remove a rehearsal temp tree, including sealed (read-only) directories copied from the root: rmtree cannot
    unlink the entries of a directory without owner write permission. Never follows symlinks. True when gone."""
    for dirpath, dirnames, _ in os.walk(path):
        for d in [dirpath] + [os.path.join(dirpath, n) for n in dirnames]:
            try:
                st = os.lstat(d)
                if stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_IRWXU != stat.S_IRWXU:
                    os.chmod(d, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


class ProjectLayoutError(ValueError):
    """The projects/ tree of a root is not what the canonical comparison can trust; rehearsal must fail closed."""


def _project_dirs(root):
    """{slug: manifest} for every project directory under root/projects, failing closed on anything malformed.

    Non-directory entries (Finder's .DS_Store, AppleDouble ._* files, stray notes, symlinks to files, dangling
    links) are not projects and are skipped; pathlib's "*/" glob only restricts itself to directories on
    Python >= 3.11, so it cannot be relied on for this. Every directory, however, is a project and must hold a
    project.json that parses to a JSON object, and every registered slug must be such a directory: a malformed
    project is never silently dropped from the comparison.
    """
    projects = Path(root) / "projects"
    if not projects.is_dir():
        raise ProjectLayoutError(f"{projects} is not a directory")
    found, problems = {}, []
    for p in sorted(projects.iterdir()):
        if not p.is_dir():
            continue
        pj = p / "project.json"
        if not pj.is_file():
            problems.append(f"{p.name}: project directory without a project.json file")
            continue
        try:
            man = json.loads(pj.read_text())
        except (OSError, UnicodeDecodeError, ValueError) as e:
            problems.append(f"{p.name}: unreadable project.json ({e})")
            continue
        if not isinstance(man, dict):
            problems.append(f"{p.name}: project.json is not a JSON object")
            continue
        found[p.name] = man
    reg_path = Path(root) / "registry" / "projects.json"
    try:
        registered = json.loads(reg_path.read_text()).get("projects", {})
    except (OSError, UnicodeDecodeError, ValueError, AttributeError) as e:
        problems.append(f"registry/projects.json unreadable ({e})")
        registered = {}
    for slug in sorted(registered):
        if slug not in found and not any(x.startswith(f"{slug}: ") for x in problems):
            problems.append(f"{slug}: registered but projects/{slug} is not a project directory")
    if problems:
        raise ProjectLayoutError("; ".join(problems))
    return found


def _canonical_snapshot(root):
    """Hashes/counts of each project's canonical state, for the before/after rehearsal comparison."""
    d = {}
    for slug, man in _project_dirs(root).items():
        p = Path(root) / "projects" / slug
        events = 0
        if (p / "EVENTS.jsonl").exists():
            with open(p / "EVENTS.jsonl", "rb") as fh:
                events = sum(1 for _ in fh)
        d[slug] = {"CURRENT": sha(p / "CURRENT.md") if (p / "CURRENT.md").exists() else None,
                   "NEXT": sha(p / "NEXT.md") if (p / "NEXT.md").exists() else None,
                   "checkpoints": sorted(x.name for x in (p / "checkpoints").glob("*")) if (p / "checkpoints").exists() else [],
                   "current_checkpoint": man.get("current_checkpoint"),
                   "events_lines": events}
    return d


def rehearse(args, source_root):
    # read-only layout check of the source first: refuse before spending a full copy of the root on it
    try:
        _project_dirs(source_root)
    except ProjectLayoutError as e:
        print(f"REHEARSAL_REFUSED=malformed projects/ layout on the source root ({e})")
        print("MIGRATION_REHEARSAL=FAIL")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="aimem-rehearsal-"))
    ok = False
    try:
        ok = _rehearse_in(args, source_root, tmp)
    finally:
        # the copy holds a full duplicate of private memory: never leave it behind, including on an exception
        if args.keep:
            print(f"KEPT={tmp}")
        else:
            cleaned = _remove_tree(tmp)
            print(f"REHEARSAL_TEMP_CLEANUP={'PASS' if cleaned else 'FAIL'} {tmp}")
            ok = ok and cleaned
    print(f"MIGRATION_REHEARSAL={'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _rehearse_in(args, source_root, tmp):
    copy = tmp / "AI-Memory"
    print(f"REHEARSAL_COPY={copy}")
    print(f"REHEARSAL_SOURCE={source_root}")
    live_before = _live_manifest(source_root)
    logs_before = _append_only_snapshot(source_root)
    shutil.copytree(source_root, copy, ignore=shutil.ignore_patterns(".locks", ".run"), symlinks=True)
    try:
        iso = _isolate_copy(source_root, copy, tmp)
    except (OSError, RecursionError, shutil.Error) as e:
        print(f"REHEARSAL_REFUSED=could not isolate the copy from live data ({e})")
        return False
    print("REHEARSAL_ISOLATION=" + " ".join(f"{k}={v}" for k, v in iso.items()))
    # snapshot canonical hashes for comparison
    try:
        before = _canonical_snapshot(copy)
    except ProjectLayoutError as e:
        print(f"REHEARSAL_REFUSED=malformed projects/ layout in the rehearsal copy ({e})")
        return False
    pr = Promotion(copy, live=False, backup_dir=tmp / "backups", skip_selftest=args.skip_selftest, smoke_slug=args.smoke_slug,
                   home=tmp / "home")
    rc = pr.run()
    cmp = {"canonical_preserved": True, "detail": []}
    try:
        after = _canonical_snapshot(copy)
    except ProjectLayoutError as e:
        after = {}
        cmp["canonical_preserved"] = False
        cmp["detail"].append(f"projects/ layout broken after promotion: {e}")
    for slug in before:
        b, a = before[slug], after.get(slug, {})
        if b["CURRENT"] != a.get("CURRENT") or b["NEXT"] != a.get("NEXT"):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: CURRENT/NEXT changed")
        if slug != args.smoke_slug and b["current_checkpoint"] != a.get("current_checkpoint"):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: current checkpoint changed")
        if not set(b["checkpoints"]).issubset(set(a.get("checkpoints", []))):
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: checkpoint lost")
        if a.get("events_lines", 0) < b["events_lines"]:
            cmp["canonical_preserved"] = False
            cmp["detail"].append(f"{slug}: EVENTS shrank")
    print(f"REHEARSAL_CANONICAL_PRESERVED={cmp['canonical_preserved']} {cmp['detail'] or ''}")
    # the live root must be untouched apart from what the launchd sweep may write
    live_changes, live_expected = _live_root_changes(source_root, live_before, logs_before)
    live_ok = not live_changes
    print(f"LIVE_ROOT_EXPECTED_CONCURRENT_WRITES={live_expected}")
    print(f"LIVE_ROOT_UNTOUCHED_BY_REHEARSAL={live_ok}" + (f" CHANGES={live_changes[:10]}" if live_changes else ""))
    rep = {"rehearsal_copy": str(copy), "promotion_result": pr.report["result"], "canonical_comparison": cmp,
           "live_untouched": live_ok, "live_changes": live_changes[:200], "live_expected_concurrent_writes": live_expected,
           "isolation": iso, "gates": pr.report["gates"]}
    (DEV / "reports").mkdir(exist_ok=True)
    (DEV / "reports" / f"MIGRATION_REHEARSAL_{time.strftime('%Y%m%dT%H%M%S%z')}.json").write_text(json.dumps(rep, indent=2))
    return rc == 0 and cmp["canonical_preserved"] and live_ok


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
    ap.add_argument(
        "--smoke-slug",
        default="ai-memory",
        help="registered project used for the begin/step/finish smoke test (admin checkpoint, CURRENT not advanced)",
    )

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
        smoke_slug=a.smoke_slug,
    )

    raise SystemExit(pr.run())


if __name__ == "__main__":
    main()
