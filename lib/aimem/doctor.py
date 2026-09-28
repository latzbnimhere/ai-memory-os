"""Doctor V4: diagnose without mutating (repair only with --repair)."""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import sqlite3
import time
from pathlib import Path

from . import core, index, launchagent, objects, provenance, recover, sessions, txn
from .core import CONFIG, DB, ROOT, config, project_dir, registry, try_load_json

REQUIRED = ["project.json", "CURRENT.md", "NEXT.md", "DECISIONS.jsonl", "EVENTS.jsonl", "ARTIFACTS.jsonl"]


def _jsonl_valid(path):
    bad = []
    if not path.exists():
        return bad
    with path.open("r", errors="ignore") as f:
        for i, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                json.loads(line)
            except Exception:
                bad.append(i)
    return bad


TMP_MAX_AGE_S = 3600  # atomic_write temp files older than this belong to a dead writer


def repair_jsonl(path: Path, qdir: Path):
    """Quarantine unparseable lines of an append-only journal. Returns number of lines removed.

    Runs under the same flock that append_jsonl uses and rewrites the file in place (same
    inode), so concurrent O_APPEND writers are never lost. Before rewriting, the full
    original is saved as <name>.<stamp>.orig and the bad lines as <name>.<stamp>.bad in
    cold/quarantine/ (neither is indexed). Nothing is discarded.
    """
    fd = os.open(str(path), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with os.fdopen(os.dup(fd), "rb") as f:
                raw = f.read()
            good, bad = [], []
            for line in raw.split(b"\n"):
                if not line.strip():
                    continue
                try:
                    json.loads(line.decode("utf-8"))
                    good.append(line)
                except Exception:
                    bad.append(line)
            if not bad:
                return 0
            qdir.mkdir(parents=True, exist_ok=True)
            tag = f"{path.name}.{core.stamp()}"
            for suffix, data in ((".orig", raw), (".bad", b"\n".join(bad) + b"\n")):
                with open(qdir / (tag + suffix), "wb") as q:
                    q.write(data)
                    q.flush()
                    os.fsync(q.fileno())
            core._fsync_dir(qdir)
            data = b"".join(line + b"\n" for line in good)
            os.lseek(fd, 0, os.SEEK_SET)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.ftruncate(fd, len(data))
            os.fsync(fd)
            return len(bad)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _stale_temp_files(base: Path):
    now = time.time()
    out = []
    for f in base.rglob(".*.tmp"):
        try:
            if f.is_file() and now - f.stat().st_mtime > TMP_MAX_AGE_S:
                out.append(f)
        except OSError:
            pass
    return out


def run(deep=False, check_repo=True, repair=False, slug_filter=None):
    core.ensure_root()
    cfg = config()
    errors, warnings, info = [], [], []
    reg = registry()
    if not CONFIG.exists():
        warnings.append("config.json missing; defaults are active.")
    # registry sanity: unsafe slugs, recursive indexing risk, ambiguous repo registrations
    unsafe = set()
    by_repo = {}
    for slug, rec in reg["projects"].items():
        try:
            core.safe_component(slug, "slug")
        except SystemExit:
            errors.append(f"registry: unsafe slug {slug!r} (path separator / leading dot); fix registry/projects.json manually")
            unsafe.add(slug)
            continue
        if core.recursive_index_risk(rec.get("repo")):
            errors.append(f"{slug}: repo path would recursively index the memory root: {rec.get('repo')}")
        if (rec.get("repo") or "").strip():
            try:
                key = str(Path(rec["repo"]).expanduser().resolve())
            except Exception:
                key = rec["repo"]
            by_repo.setdefault(key, []).append(slug)
    for repo_path, slugs_ in sorted(by_repo.items()):
        if len(slugs_) > 1:
            warnings.append(f"registry: repo {repo_path} is registered under several slugs ({', '.join(sorted(slugs_))}); "
                            "project detection fails closed for it")
    # writable
    for d in (ROOT, ROOT / "registry", core.PROJECTS, core.LOCKS):
        if d.exists() and not os.access(d, os.W_OK):
            errors.append(f"not writable: {d}")
    slugs = [s for s in reg["projects"] if (not slug_filter or s == slug_filter) and s not in unsafe]
    repaired_journals = []
    for slug in slugs:
        p = project_dir(slug)
        if not p.exists():
            errors.append(f"{slug}: registered but project directory missing")
            continue
        for req in REQUIRED:
            if not (p / req).exists():
                errors.append(f"{slug}: missing {req}")
        man = try_load_json(p / "project.json", None)
        if man is None:
            errors.append(f"{slug}: project.json invalid JSON")
            man = {}
        if man.get("slug") and man.get("slug") != slug:
            errors.append(f"{slug}: project.json slug mismatch ({man.get('slug')})")
        if (reg["projects"][slug].get("repo") or "") != (man.get("repo") or ""):
            warnings.append(f"{slug}: registry repo differs from project.json repo (contradictory metadata)")
        try:
            c = (p / "CURRENT.md").read_text(errors="ignore")
            if len(c) > cfg["max_current_chars_warning"]:
                warnings.append(f"{slug}: CURRENT.md is large ({len(c)} chars); token efficiency may degrade.")
            if "GENERATED AI CONTEXT PACK" in c or "NOT CANONICAL" in c[:400]:
                errors.append(f"{slug}: CURRENT.md contains generated-context contamination")
        except Exception:
            pass
        cp = man.get("current_checkpoint")
        if cp:
            cpd = p / "checkpoints" / cp
            meta = cpd / "meta.json"
            if not meta.exists():
                errors.append(f"{slug}: current checkpoint metadata missing: {cp}")
            elif deep:
                m = try_load_json(meta, None)
                if m is None:
                    errors.append(f"{slug}: checkpoint meta.json invalid: {cp}")
                else:
                    cpc = cpd / "CURRENT.md"
                    if cpc.exists() and m.get("current_sha256") and core.sha256_file(cpc) != m["current_sha256"]:
                        errors.append(f"{slug}: checkpoint CURRENT hash mismatch: {cp}")
                    cpn = cpd / "NEXT.md"
                    if cpn.exists() and m.get("next_sha256") and core.sha256_file(cpn) != m["next_sha256"]:
                        errors.append(f"{slug}: checkpoint NEXT hash mismatch: {cp}")
        if deep:
            for meta in (p / "checkpoints").glob("*/meta.json") if (p / "checkpoints").exists() else []:
                m = try_load_json(meta, None)
                if m is None:
                    errors.append(f"{slug}: invalid checkpoint meta {meta.parent.name}")
                    continue
                if m.get("id") != meta.parent.name:
                    errors.append(f"{slug}: checkpoint id mismatch in {meta.parent.name}")
                ev = core.version_tuple(m.get("engine_version"))
                if ev and ev >= (4, 0, 0) and "txn" not in m and meta.parent.name != man.get("current_checkpoint"):
                    # pre-4.2 engines wrote meta.json before the CAS-checked commit and only added txn/memory_version
                    # after it succeeded, so a V4 checkpoint without "txn" is the leftover of a refused finish.
                    warnings.append(f"{slug}: checkpoint {meta.parent.name} was never committed (left by a refused/conflicted "
                                    "finish in an older engine); not authoritative, safe to archive manually")
                if isinstance(m.get("memory_version"), int) and m["memory_version"] > int(man.get("memory_version", 0) or 0):
                    errors.append(f"{slug}: checkpoint {meta.parent.name} claims memory_version {m['memory_version']} "
                                  f"> project {man.get('memory_version')} (uncommitted checkpoint; check `aimem txn {slug}`)")
                cpc = meta.parent / "CURRENT.md"
                if cpc.exists() and m.get("current_sha256") and core.sha256_file(cpc) != m["current_sha256"]:
                    errors.append(f"{slug}: checkpoint CURRENT hash mismatch: {meta.parent.name}")
        if (p / "checkpoints").exists():
            for cpd in sorted(x for x in (p / "checkpoints").iterdir() if x.is_dir() and not x.name.startswith(".")):
                if not (cpd / "meta.json").exists() and cpd.name != man.get("current_checkpoint"):
                    warnings.append(f"{slug}: incomplete checkpoint {cpd.name} (no meta.json; interrupted before sealing; "
                                    "ignored by index/context; safe to archive manually)")
        journals = [p / n for n in ["DECISIONS.jsonl", "EVENTS.jsonl", "ARTIFACTS.jsonl", "PROVENANCE.jsonl"]]
        if deep and (p / "cold").exists():
            journals += sorted(f for f in (p / "cold").rglob("*.jsonl") if "quarantine" not in f.parts)
        for jf in journals:
            bad = _jsonl_valid(jf)
            if bad and repair:
                n = repair_jsonl(jf, p / "cold" / "quarantine")
                repaired_journals.append(f"{slug}: quarantined {n} invalid line(s) from {jf.relative_to(p)} -> cold/quarantine/")
                bad = _jsonl_valid(jf)
            for i in bad[:5]:
                errors.append(f"{slug}: invalid JSONL {jf.relative_to(p)}:{i} (torn/corrupt line; `aimem doctor --repair` "
                              "quarantines it with the original preserved)")
            for f in (p / "sessions").glob("*.json") if (p / "sessions").exists() else []:
                if try_load_json(f, None) is None:
                    errors.append(f"{slug}: invalid session JSON {f.name}")
        # transactions / staged orphans
        pend = txn.inspect(slug)
        for t in pend:
            errors.append(f"{slug}: unresolved transaction {t['id']} state={t['state']} resolution={t['resolution']}")
        for f in list(p.glob(".*.staged")) + list(p.glob("checkpoints/*/.*.staged")):
            if not any(str(f) in (e.get("staged", "") for e in (try_load_json(Path(t["path"]), {}) or {}).get("entries", [])) for t in pend):
                warnings.append(f"{slug}: orphan staged file {f.name}")
        # sessions / leases
        opens = sessions.open_sessions(slug)
        for f, j in opens:
            st = sessions.lease_state(j)
            if st == "ABANDONED":
                warnings.append(f"{slug}: session {j.get('id')} lease ABANDONED")
            elif st == "STALE":
                warnings.append(f"{slug}: session {j.get('id')} lease STALE")
        if len(opens) > 1:
            info.append(f"{slug}: {len(opens)} open sessions (explicit --session required)")
        for a in recover.scan(slug) if check_repo else []:
            if a["recovery_required"]:
                warnings.append(f"{slug}: RECOVERY_REQUIRED session {a['id']} ({'; '.join(a['reasons'])})")
        # secrets
        if cfg.get("secret_scan") and deep:
            for f in [p / "CURRENT.md", p / "NEXT.md", p / "DECISIONS.jsonl", p / "PROVENANCE.jsonl"]:
                if f.exists():
                    for hit in core.secret_hits(f):
                        warnings.append(f"{slug}: possible secret in {f.name}: {hit}")
        # artifact references
        for rec in core.read_jsonl(p / "ARTIFACTS.jsonl"):
            sp = rec.get("stored_path")
            if sp and not Path(sp).exists() and not rec.get("object"):
                warnings.append(f"{slug}: broken artifact reference {sp}")
        if check_repo:
            rs = core.repo_state_for(slug)
            if rs.get("configured") and not rs.get("exists"):
                warnings.append(f"{slug}: configured repo path is missing: {rs.get('path')}")
            if deep:
                warnings += provenance.warnings(slug, rs)
    # temp files left behind by writers that died mid atomic_write
    stale_tmp = _stale_temp_files(ROOT / "projects") + _stale_temp_files(ROOT / "registry")
    for f in stale_tmp:
        if repair:
            try:
                f.unlink()
                info.append(f"removed stale temp file {f.relative_to(ROOT)}")
            except OSError:
                warnings.append(f"stale temp file (could not remove) {f.relative_to(ROOT)}")
        else:
            warnings.append(f"stale temp file {f.relative_to(ROOT)} (interrupted write; never canonical; `doctor --repair` removes)")
    info += repaired_journals
    # objects
    if deep:
        ok, bad, misplaced = objects.verify_all()
        for path, exp, act in bad:
            errors.append(f"object hash mismatch {path}")
        for path in misplaced:
            errors.append(f"object misplaced {path}")
        for sha, where in objects.broken_references():
            errors.append(f"broken object reference {sha[:12]} from {', '.join(where[:3])}")
        info.append(f"objects verified={ok}")
    # DB
    dbh = index.db_health()
    if index.db_is_broken(dbh):
        errors.append(f"memory.db BROKEN {dbh.splitlines()[0][:200]} (derived index; `aimem doctor --repair` "
                      "quarantines and rebuilds it from canonical files)")
    elif dbh.startswith(index.DB_UNAVAILABLE_PREFIX):
        warnings.append(f"memory.db {dbh[:200]} (busy/locked; not treated as damage; re-run doctor)")
    # LaunchAgent (found by the aimem-sweep it runs: older installations use their own label)
    _, la_plist = launchagent.find(ROOT)
    if la_plist.exists():
        try:
            pl = plistlib.loads(la_plist.read_bytes())
            args = pl.get("ProgramArguments", [])
            if not any(str(a).endswith("aimem-sweep") for a in args):
                warnings.append("LaunchAgent does not run aimem-sweep")
            elif not Path(args[-1]).exists():
                errors.append(f"LaunchAgent target missing: {args[-1]}")
        except Exception as e:
            errors.append(f"LaunchAgent plist unreadable: {e}")
    else:
        info.append("LaunchAgent not installed (optional)")
    if repair:
        for slug in slugs:
            for rid, action in txn.repair(slug):
                info.append(f"{slug}: txn {rid} -> {action}")
            # report the state AFTER repair: resolved transactions are no longer errors, manual ones still are
            errors = [e for e in errors if not e.startswith(f"{slug}: unresolved transaction ")]
            for t in txn.inspect(slug):
                errors.append(f"{slug}: unresolved transaction {t['id']} state={t['state']} resolution={t['resolution']}")
        if index.db_is_broken(dbh) or dbh == index.DB_MISSING:
            with core.lock("index", timeout=120):
                # re-check under the lock: a concurrent search/reindex may already have healed it, and a
                # healthy index must never be quarantined
                now = index.db_health()
                if index.db_is_broken(now):
                    index._quarantine_db(now.splitlines()[0][:200])
            if index.db_is_broken(now) or now == index.DB_MISSING:
                index.reindex(None, quiet=True, full=True)
            after = index.db_health()
            info.append(f"memory.db rebuilt (health {after})" if now != index.DB_OK else "memory.db already healthy (healed concurrently)")
            if after == index.DB_OK:
                errors = [e for e in errors if not e.startswith("memory.db")]
    return errors, warnings, info


def print_report(errors, warnings, info):
    if errors:
        print("DOCTOR=FAIL")
    else:
        print("DOCTOR=PASS")
    for e in errors:
        print("ERROR:", e)
    for w in warnings:
        print("WARN:", w)
    for i in info:
        print("INFO:", i)
    return 1 if errors else 0
