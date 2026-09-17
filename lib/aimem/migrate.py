"""Idempotent V3.1.1 -> V4 layout migration of a memory root (never deletes history)."""
from __future__ import annotations

from pathlib import Path

from . import core, index, provenance, sessions
from .core import OBJECTS, ROOT, RUN, atomic_write, atomic_write_json, config, iso, project_dir, registry, try_load_json


def plan_and_apply(dry_run=False):
    core.ensure_root()
    actions = []
    reg = registry()
    for d in (OBJECTS / "sha256", RUN):
        if not d.exists():
            actions.append(f"mkdir {d.relative_to(ROOT)}")
            if not dry_run:
                d.mkdir(parents=True, exist_ok=True)
    for slug in sorted(reg["projects"]):
        p = project_dir(slug)
        if not p.exists():
            actions.append(f"{slug}: WARN project dir missing (left as is)")
            continue
        for sub in ("cold/step-journal", "cold/summaries", "knowledge", "artifacts", "checkpoints", "sessions", ".txn"):
            if not (p / sub).exists():
                actions.append(f"{slug}: mkdir {sub}")
                if not dry_run:
                    (p / sub).mkdir(parents=True, exist_ok=True)
        man = try_load_json(p / "project.json", None)
        if man is None:
            actions.append(f"{slug}: ERROR project.json invalid; not migrated")
            continue
        changed = False
        if "memory_version" not in man:
            man["memory_version"] = 1
            changed = True
        if "engine_version" not in man or man.get("engine_version") != core.VERSION:
            man["engine_version"] = core.VERSION
            changed = True
        if "migrated_from" not in man:
            man["migrated_from"] = "V3.1.1"
            man["migrated_at"] = iso()
            changed = True
        if changed:
            actions.append(f"{slug}: project.json += memory_version/engine_version/migration provenance")
            if not dry_run:
                atomic_write_json(p / "project.json", man)
        if not (p / "PROVENANCE.jsonl").exists():
            actions.append(f"{slug}: create PROVENANCE.jsonl (seed from REPO_STATE.json + current checkpoint)")
            if not dry_run:
                (p / "PROVENANCE.jsonl").touch()
                rs = try_load_json(p / "REPO_STATE.json", {}) or {}
                if rs.get("is_git_repo"):
                    provenance.record_physical_git(slug, rs, session=None, source_ref="migration:REPO_STATE.json")
                if man.get("current_checkpoint"):
                    provenance.record_fact(slug, "checkpoint.current", man["current_checkpoint"], "CHECKPOINT",
                                           source_ref="migration:project.json", status="VERIFIED", note="migrated from V3.1.1 manifest")
        # open sessions: add conservative lease (last_heartbeat = last step time or start)
        for f, j in sessions.open_sessions(slug):
            if "lease" not in j:
                last = sessions.last_step(slug, j.get("id"))
                hb = (last or {}).get("time") or j.get("started_at")
                actions.append(f"{slug}: session {j.get('id')} += lease (last_heartbeat={hb})")
                if not dry_run:
                    cfg = config()
                    j["lease"] = {"state": "MIGRATED", "last_heartbeat": hb, "heartbeats": 0,
                                  "stale_after_s": cfg["heartbeat_stale_s"], "abandoned_after_s": cfg["heartbeat_abandoned_s"], "migrated": True}
                    j.setdefault("start_memory_version", 0)
                    atomic_write_json(f, j)
    cfg = try_load_json(core.CONFIG, {}) or {}
    if cfg.get("version") != 4:
        merged = dict(core.DEFAULT_CONFIG)
        merged.update(cfg)
        merged["version"] = 4
        actions.append("config.json -> version 4 (existing keys preserved)")
        if not dry_run:
            atomic_write_json(core.CONFIG, merged)
    if (ROOT / "VERSION").read_text().strip() != core.VERSION if (ROOT / "VERSION").exists() else True:
        actions.append(f"VERSION -> {core.VERSION}")
        if not dry_run:
            atomic_write(ROOT / "VERSION", core.VERSION + "\n")
    pl = ROOT / "PATCH_LEVEL"
    if not pl.exists() or core.PATCH_LEVEL not in pl.read_text():
        actions.append(f"PATCH_LEVEL -> {core.PATCH_LEVEL} (lineage preserved)")
        if not dry_run:
            prev = pl.read_text().strip() if pl.exists() else ""
            atomic_write(pl, f"AI_MEMORY_OS_PATCH={core.PATCH_LEVEL}\nAI_MEMORY_OS_VERSION={core.VERSION}\nAPPLIED_AT={iso()}\n"
                              f"LINEAGE={' -> '.join(core.ENGINE_LINEAGE)}\nPREVIOUS:\n{prev}\n")
    gi = ROOT / ".gitignore"
    want = ["registry/memory.db", "registry/memory.db-wal", "registry/memory.db-shm", ".locks/", ".run/", "projects/*/.generated/", "projects/*/.txn/", "**/*.staged"]
    if gi.exists():
        lines = gi.read_text().splitlines()
        missing = [w for w in want if w not in lines]
        if missing:
            actions.append(".gitignore += " + ", ".join(missing))
            if not dry_run:
                atomic_write(gi, "\n".join(lines + missing) + "\n")
    if not dry_run:
        index.reindex(None, quiet=True)
        actions.append("reindex (schema upgraded with mtime column)")
        core.rebuild_master_index()
    return actions
