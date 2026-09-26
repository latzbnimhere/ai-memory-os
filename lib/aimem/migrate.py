"""Idempotent V3.1.1 -> V4 layout migration of a memory root (never deletes history).

Safety rules:
- Never downgrade: if the root, config or any project manifest was written by a NEWER
  engine (or an unknown future schema), migration refuses before changing anything
  (dry-run included). Upgrade the engine instead.
- Only additive changes: missing directories/files/keys are created; nothing is removed.
- Manifest updates run under the project write lock; session updates under the session lock.
"""
from __future__ import annotations

from pathlib import Path

from . import core, index, provenance, sessions
from .core import OBJECTS, ROOT, RUN, atomic_write, atomic_write_json, config, iso, project_dir, registry, try_load_json


SUPPORTED_CONFIG_VERSION = 4


def _safe(slug):
    try:
        core.safe_component(slug, "slug")
        return True
    except SystemExit:
        return False


def future_blockers():
    """Reasons this engine must not migrate the root (data from a newer engine / unknown schema)."""
    out = []
    vf = ROOT / "VERSION"
    if vf.exists():
        rv = vf.read_text().strip()
        # legacy V3 roots used "2.0.0+V3.1.1"-style strings: numerically older, so still migratable
        if core.version_not_older(rv) and rv != core.VERSION:
            out.append(f"root VERSION {rv[:40]!r} is newer than (or a different build of) engine {core.VERSION}, or unparseable")
    cfg = try_load_json(core.CONFIG, {}) or {}
    cv = cfg.get("version")
    if cv is not None and (not isinstance(cv, int) or cv > SUPPORTED_CONFIG_VERSION):
        out.append(f"config.json version {cv!r} is newer than supported {SUPPORTED_CONFIG_VERSION}")
    for slug in sorted(registry()["projects"]):
        try:
            man = try_load_json(project_dir(slug) / "project.json", None)
        except SystemExit:
            out.append(f"{slug}: unsafe slug in registry")
            continue
        if not man or not man.get("engine_version"):
            continue
        pv = str(man["engine_version"])
        if core.version_not_older(pv) and pv != core.VERSION:
            out.append(f"{slug}: project.json engine_version {pv[:40]} is newer than (or a different build of) engine {core.VERSION}")
    return out


def plan_and_apply(dry_run=False):
    core.ensure_root()
    blockers = future_blockers()
    if blockers:
        core.die("MIGRATE_REFUSED_NEWER_DATA: " + "; ".join(blockers[:10]) +
                 ". This engine will not rewrite data from a newer engine or unknown schema; install the newer engine.")
    from . import txn
    pend = [s for s in sorted(registry()["projects"]) if _safe(s) and txn.pending(s)]
    if pend:
        core.die("MIGRATE_REFUSED_PENDING_TRANSACTIONS: " + ", ".join(pend) + ". Rewriting project.json now could make a "
                 "roll-forward impossible; resolve with `aimem txn <slug> --repair` first.", core.EXIT_RECOVERY_REQUIRED)
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
        with core.project_write_lock(slug):
            man = try_load_json(p / "project.json", None)
            if man is None:
                actions.append(f"{slug}: ERROR project.json invalid; not migrated")
                continue
            changed = []
            if "memory_version" not in man:
                # A manifest without memory_version predates V4: record where it came from.
                man["memory_version"] = 1
                man.setdefault("migrated_from", "V3.1.1")
                man.setdefault("migrated_at", iso())
                changed.append("memory_version+migration provenance")
            if man.get("engine_version") != core.VERSION:
                changed.append(f"engine_version {man.get('engine_version')} -> {core.VERSION}")
                man["engine_version"] = core.VERSION
            if changed:
                actions.append(f"{slug}: project.json " + ", ".join(changed))
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
                    with core.project_session_lock(slug):
                        j = try_load_json(f, None) or j
                        if "lease" not in j:
                            j["lease"] = {"state": "MIGRATED", "last_heartbeat": hb, "heartbeats": 0,
                                          "stale_after_s": cfg["heartbeat_stale_s"],
                                          "abandoned_after_s": cfg["heartbeat_abandoned_s"], "migrated": True}
                            j.setdefault("start_memory_version", 0)
                            atomic_write_json(f, j)
    cfg = try_load_json(core.CONFIG, {}) or {}
    if cfg.get("version") != SUPPORTED_CONFIG_VERSION:  # only ever older here (newer refused above)
        merged = dict(core.DEFAULT_CONFIG)
        merged.update(cfg)
        merged["version"] = SUPPORTED_CONFIG_VERSION
        actions.append(f"config.json -> version {SUPPORTED_CONFIG_VERSION} (existing keys preserved)")
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
