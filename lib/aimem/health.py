"""aimem health — compact system/project health with process exit codes."""
from __future__ import annotations

import time
from pathlib import Path

from . import backup, core, doctor, index, provenance, recover, reconcile, sessions, txn
from .core import ROOT, config, project_dir, registry


def collect(slug_filter=None, check_repo=True):
    core.ensure_root()
    cfg = config()
    reg = registry()
    slugs = [s for s in sorted(reg["projects"]) if not slug_filter or s == slug_filter]
    h = {"AI_MEMORY_HEALTH": "OK", "version": core.VERSION, "installed_version": core.installed_version(),
         "patch_level": (ROOT / "PATCH_LEVEL").read_text().strip().splitlines()[0] if (ROOT / "PATCH_LEVEL").exists() else "-",
         "root": str(ROOT), "projects": len(reg["projects"]), "active_sessions": 0, "stale_sessions": 0, "abandoned_sessions": 0,
         "recovery_required": 0, "ambiguous_sessions": 0, "unresolved_transactions": 0, "provenance_warnings": 0,
         "context_size_warnings": 0, "match_status": {}, "per_project": {}}
    issues = []
    for slug in slugs:
        if not core.project_exists(slug):
            issues.append(f"{slug}: project dir missing")
            continue
        opens = sessions.open_sessions(slug)
        states = [sessions.lease_state(j, cfg) for f, j in opens]
        h["active_sessions"] += states.count("ACTIVE")
        h["stale_sessions"] += states.count("STALE")
        h["abandoned_sessions"] += states.count("ABANDONED")
        if len(opens) > 1:
            h["ambiguous_sessions"] += 1
        pend = txn.inspect(slug)
        h["unresolved_transactions"] += len(pend)
        rec_req = 0
        live = core.repo_state_for(slug) if check_repo else {"configured": False}
        if check_repo:
            for a in recover.scan(slug):
                if a["recovery_required"]:
                    rec_req += 1
            rc = reconcile.reconcile(slug, live)
            h["match_status"][slug] = rc["status"] + ("+" + "+".join(rc["flags"]) if rc["flags"] else "")
            pw = provenance.warnings(slug, live)
            h["provenance_warnings"] += len(pw)
        h["recovery_required"] += rec_req
        p = project_dir(slug)
        cur = (p / "CURRENT.md").read_text(errors="ignore") if (p / "CURRENT.md").exists() else ""
        nxt = (p / "NEXT.md").read_text(errors="ignore") if (p / "NEXT.md").exists() else ""
        ctx_est = core.approx_tokens(cur + nxt) + 600
        if ctx_est > cfg.get("context_size_warning_tokens", 9000):
            h["context_size_warnings"] += 1
        man = core.try_load_json(p / "project.json", {}) or {}
        h["per_project"][slug] = {"checkpoint": man.get("current_checkpoint"), "memory_version": man.get("memory_version", 0),
                                  "open_sessions": len(opens), "lease_states": states, "recovery_required": rec_req,
                                  "unresolved_txn": len(pend), "context_estimate_tokens": ctx_est, "memory_bytes": core.dir_size(p),
                                  "checkpoints": len(list((p / "checkpoints").glob("*/meta.json"))) if (p / "checkpoints").exists() else 0}
    # global checks
    h["database"] = index.db_health()
    errors, warnings, info = doctor.run(deep=False, check_repo=check_repo, slug_filter=slug_filter)
    h["doctor_errors"] = len(errors)
    h["doctor_warnings"] = len(warnings)
    h["journal_health"] = "OK" if not any("invalid JSONL" in e for e in errors) else "INVALID_JSONL"
    h["checkpoint_integrity"] = "OK" if not any("checkpoint" in e for e in errors) else "FAIL"
    la = doctor.LAUNCH_AGENT
    h["launchagent"] = "INSTALLED" if la.exists() else "NOT_INSTALLED"
    if la.exists():
        import subprocess
        try:
            r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=10)
            h["launchagent"] = "LOADED" if "io.aimemory.sweep" in r.stdout else "INSTALLED_NOT_LOADED"
        except Exception:
            pass
    nb = backup.newest()
    if nb:
        h["backup_age_hours"] = nb["age_hours"]
        h["backup_status"] = ("OK" if nb["age_hours"] <= cfg.get("backup_max_age_days", 7) * 24 else "OLD") + ("_VERIFIED" if nb.get("verified") == "PASS" else "_UNVERIFIED")
        h["backup_newest"] = nb["name"]
    else:
        h["backup_age_hours"] = None
        h["backup_status"] = "NONE"
    h["disk_usage"] = core.human_bytes(core.dir_size(ROOT))
    try:
        import shutil
        h["disk_free"] = core.human_bytes(shutil.disk_usage(str(ROOT)).free)
    except Exception:
        h["disk_free"] = "-"
    # overall
    level = 0
    if h["recovery_required"] or h["unresolved_transactions"] or h["doctor_errors"] or h["database"].startswith("CORRUPT") or h["checkpoint_integrity"] != "OK":
        level = 2
    elif h["stale_sessions"] or h["abandoned_sessions"] or h["ambiguous_sessions"] or h["doctor_warnings"] or h["backup_status"] in ("NONE", "OLD_UNVERIFIED", "OLD_VERIFIED", "OK_UNVERIFIED") \
            or h["provenance_warnings"] or h["context_size_warnings"] or any(_needs_attention(v) for v in h["match_status"].values()):
        level = 1
    h["AI_MEMORY_HEALTH"] = {0: "OK", 1: "WARN", 2: "FAIL"}[level]
    h["exit_code"] = {0: core.EXIT_OK, 1: core.EXIT_WARN, 2: core.EXIT_RECOVERY_REQUIRED if (h["recovery_required"] or h["unresolved_transactions"]) else core.EXIT_ERROR}[level]
    h["issues"] = issues + errors[:10] + warnings[:10]
    return h


def _needs_attention(v):
    """RUNTIME_VERIFICATION_REQUIRED for a project without a repo is informational, not a warning."""
    return v.startswith(("PHYSICAL_AHEAD", "DIVERGED", "MEMORY_AHEAD_OR_UNVERIFIED")) or "+" in v


def render(h, verbose=False):
    keys = ["AI_MEMORY_HEALTH", "version", "installed_version", "patch_level", "projects", "active_sessions", "stale_sessions",
            "abandoned_sessions", "recovery_required", "ambiguous_sessions", "unresolved_transactions", "database", "journal_health",
            "checkpoint_integrity", "launchagent", "backup_status", "backup_age_hours", "disk_usage", "disk_free",
            "context_size_warnings", "provenance_warnings", "doctor_errors", "doctor_warnings"]
    L = [f"{k}={h.get(k)}" for k in keys]
    L.append("memory_physical_match=" + (",".join(f"{k}:{v}" for k, v in h["match_status"].items()) or "-"))
    if verbose:
        for slug, v in h["per_project"].items():
            L.append(f"project.{slug}=checkpoint:{v['checkpoint']} v{v['memory_version']} open:{v['open_sessions']} leases:{','.join(v['lease_states']) or '-'} "
                     f"recovery:{v['recovery_required']} txn:{v['unresolved_txn']} ctx~{v['context_estimate_tokens']}tok size:{core.human_bytes(v['memory_bytes'])} checkpoints:{v['checkpoints']}")
        for i in h["issues"]:
            L.append(f"issue={i}")
    return "\n".join(L)
