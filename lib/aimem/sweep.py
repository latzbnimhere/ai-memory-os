"""aimem-sweep: passive physical-change journal (launchd, every 120s). Records; never infers semantics."""
from __future__ import annotations

from pathlib import Path

from . import core, sessions
from .core import ROOT, atomic_write_json, day, iso, project_dir, sha256_file, try_load_json


def watched_sources(repo: Path):
    out = {}
    for p in (repo / "docs" / "CURRENT_CHECKPOINT.md", repo / "docs" / "NEXT_TASK.md", repo / "CURRENT_CHECKPOINT.md", repo / "NEXT_TASK.md"):
        if p.exists() and p.is_file():
            try:
                out[str(p)] = {"sha256": sha256_file(p), "bytes": p.stat().st_size, "mtime": p.stat().st_mtime}
            except Exception:
                pass
    return out


def main(argv=None):
    reg = try_load_json(core.REGISTRY, {"projects": {}}) or {"projects": {}}
    cfg = core.config()
    changed = 0
    lease_summary = {}
    for slug, rec in reg.get("projects", {}).items():
        p = project_dir(slug)
        if not p.exists():
            continue
        opens = []
        try:
            opens = sessions.open_sessions(slug)
            states = [sessions.lease_state(j, cfg) for f, j in opens]
            if states:
                lease_summary[slug] = states
        except Exception:
            pass
        repo_s = (rec.get("repo") or "").strip()
        if not repo_s:
            continue
        repo = Path(repo_s).expanduser()
        rs = core.repo_state_for_path(repo_s)
        rs.pop("configured", None)
        state = {"captured_at": iso(), "repo": rs, "watched_sources": watched_sources(repo)}
        state_path = p / "AUTO_PHYSICAL_STATE.json"
        old = try_load_json(state_path, {}) or {}
        comparable = {"repo": {k: v for k, v in rs.items() if k != "captured_at"}, "watched_sources": state["watched_sources"]}
        old_comp = {"repo": {k: v for k, v in (old.get("repo") or {}).items() if k != "captured_at"}, "watched_sources": old.get("watched_sources")}
        if comparable != old_comp:
            core.append_jsonl(p / "cold" / "physical-journal" / f"{day()}.jsonl", {
                "time": iso(), "kind": "auto_physical_change", "project": slug, "previous": old_comp if old else None,
                "current": comparable, "policy": "physical-change journal; no semantic inference"})
            core.append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "auto_physical_change", "project": slug,
                                                    "summary": "Physical repository or watched continuation-source state changed.",
                                                    "repo_head": rs.get("head"), "repo_dirty": rs.get("dirty")})
            # Fresh physical activity can extend a lease only when
            # attribution is unambiguous and the session is already ACTIVE.
            # Never revive STALE/ABANDONED sessions and never guess between
            # concurrent sessions.
            if (
                old
                and cfg.get(
                    "sweep_refresh_unique_active_session_on_physical_change",
                    True,
                )
                and len(opens) == 1
            ):
                _, sj = opens[0]
                if sessions.lease_state(sj, cfg) == "ACTIVE":
                    try:
                        sessions.touch_heartbeat(
                            slug,
                            sj.get("id"),
                            note="sweep: fresh physical project activity observed",
                        )
                    except Exception:
                        pass

            changed += 1
        atomic_write_json(state_path, state)
    try:
        core.RUN.mkdir(exist_ok=True)
        atomic_write_json(core.RUN / "sweep-state.json", {"time": iso(), "changed_projects": changed, "leases": lease_summary, "version": core.VERSION})
    except Exception:
        pass
    print(f"SWEEP=PASS changed_projects={changed}")
