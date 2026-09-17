"""Doctor V4: diagnose without mutating (repair only with --repair)."""
from __future__ import annotations

import json
import os
import plistlib
import sqlite3
from pathlib import Path

from . import core, index, objects, provenance, recover, sessions, txn
from .core import CONFIG, DB, ROOT, config, project_dir, registry, try_load_json

REQUIRED = ["project.json", "CURRENT.md", "NEXT.md", "DECISIONS.jsonl", "EVENTS.jsonl", "ARTIFACTS.jsonl"]
LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "io.aimemory.sweep.plist"


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


def run(deep=False, check_repo=True, repair=False, slug_filter=None):
    core.ensure_root()
    cfg = config()
    errors, warnings, info = [], [], []
    reg = registry()
    if not CONFIG.exists():
        warnings.append("config.json missing; defaults are active.")
    # recursive indexing risk
    for slug, rec in reg["projects"].items():
        if core.recursive_index_risk(rec.get("repo")):
            errors.append(f"{slug}: repo path would recursively index the memory root: {rec.get('repo')}")
    # writable
    for d in (ROOT, ROOT / "registry", core.PROJECTS, core.LOCKS):
        if d.exists() and not os.access(d, os.W_OK):
            errors.append(f"not writable: {d}")
    slugs = [s for s in reg["projects"] if not slug_filter or s == slug_filter]
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
                cpc = meta.parent / "CURRENT.md"
                if cpc.exists() and m.get("current_sha256") and core.sha256_file(cpc) != m["current_sha256"]:
                    errors.append(f"{slug}: checkpoint CURRENT hash mismatch: {meta.parent.name}")
        for name in ["DECISIONS.jsonl", "EVENTS.jsonl", "ARTIFACTS.jsonl", "PROVENANCE.jsonl"]:
            bad = _jsonl_valid(p / name)
            for i in bad[:5]:
                errors.append(f"{slug}: invalid JSONL {name}:{i}")
        if deep:
            for f in (p / "cold").rglob("*.jsonl") if (p / "cold").exists() else []:
                bad = _jsonl_valid(f)
                for i in bad[:3]:
                    errors.append(f"{slug}: invalid JSONL {f.relative_to(p)}:{i}")
            for f in (p / "sessions").glob("*.json") if (p / "sessions").exists() else []:
                if try_load_json(f, None) is None:
                    errors.append(f"{slug}: invalid session JSON {f.name}")
        # transactions / staged orphans
        pend = txn.inspect(slug)
        for t in pend:
            errors.append(f"{slug}: unresolved transaction {t['id']} state={t['state']} resolution={t['resolution']}")
        for f in p.glob(".*.staged"):
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
    if dbh.startswith("CORRUPT") or dbh.startswith("UNREADABLE"):
        errors.append(f"memory.db {dbh}")
    # LaunchAgent
    if LAUNCH_AGENT.exists():
        try:
            pl = plistlib.loads(LAUNCH_AGENT.read_bytes())
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
        if dbh != "OK":
            index.reindex(None, quiet=True)
            info.append("memory.db rebuilt")
            if index.db_health() == "OK":
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
