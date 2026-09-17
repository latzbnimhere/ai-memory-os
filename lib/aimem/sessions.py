"""Sessions, leases, heartbeats, checkpoints, finish (with compare-and-swap)."""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from . import core, provenance, reconcile, txn
from .core import (append_jsonl, atomic_write, atomic_write_json, config, iso, load_json, parse_iso,
                   project_dir, read_jsonl, sha256_file, stamp, try_load_json)

LEASE_STATES = ("ACTIVE", "STALE", "ABANDONED", "CLOSED")


def sessions_dir(slug):
    return project_dir(slug) / "sessions"


def session_file(slug, sid):
    return sessions_dir(slug) / f"{sid}.json"


def steps_file(slug, sid):
    return sessions_dir(slug) / f"{sid}.steps.jsonl"


def load_session(slug, sid):
    f = session_file(slug, sid)
    if not f.exists():
        core.die(f"Session not found: {sid}")
    return load_json(f, {}) or {}


def lease_state(j, cfg=None):
    """Derive lease state conservatively from heartbeat age."""
    if j.get("status") != "OPEN":
        return "CLOSED"
    cfg = cfg or config()
    lease = j.get("lease") or {}
    hb = parse_iso(lease.get("last_heartbeat")) or parse_iso(j.get("started_at"))
    if hb is None:
        return "STALE"
    age = (core.now() - hb).total_seconds()
    stale_after = float(lease.get("stale_after_s") or cfg["heartbeat_stale_s"])
    abandoned_after = float(lease.get("abandoned_after_s") or cfg["heartbeat_abandoned_s"])
    if age >= abandoned_after:
        return "ABANDONED"
    if age >= stale_after:
        return "STALE"
    return "ACTIVE"


def all_sessions(slug):
    d = sessions_dir(slug)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        if f.name.endswith(".steps.jsonl"):
            continue
        j = try_load_json(f, None)
        if j is None:
            continue
        out.append((f, j))
    return out


def open_sessions(slug):
    return [(f, j) for f, j in all_sessions(slug) if j.get("status") == "OPEN"]


def resolve_session(slug, explicit=None, env_ok=True):
    """Fail-closed session resolution: explicit id, else env, else the single open session."""
    import os
    candidate = explicit or (os.environ.get("AIMEM_SESSION_ID") if env_ok else None)
    if candidate:
        f = session_file(slug, candidate)
        if not f.exists():
            core.die(f"requested session does not exist: {candidate}")
        j = load_json(f, {}) or {}
        if j.get("status") != "OPEN":
            core.die(f"requested session is not OPEN: {candidate}")
        return candidate, ("explicit" if explicit else "environment")
    opens = open_sessions(slug)
    if not opens:
        return None, "none"
    if len(opens) == 1:
        return opens[0][1].get("id"), "unique_open_session"
    ids = [j.get("id", f.name) for f, j in opens]
    core.die("AMBIGUOUS_OPEN_SESSIONS: pass --session explicitly; open sessions: " + ", ".join(ids), core.EXIT_AMBIGUOUS)


def last_step(slug, sid):
    steps = read_jsonl(steps_file(slug, sid))
    return steps[-1] if steps else None


def touch_heartbeat(slug, sid, note=None):
    f = session_file(slug, sid)
    j = load_json(f, {}) or {}
    if j.get("status") != "OPEN":
        core.die(f"session is not OPEN: {sid}")
    lease = j.setdefault("lease", {})
    lease["last_heartbeat"] = iso()
    lease["heartbeats"] = int(lease.get("heartbeats", 0)) + 1
    if note:
        lease["last_note"] = core.redact(note, 300)
    lease["state"] = "ACTIVE"
    atomic_write_json(f, j)
    return j


# ---------------------------------------------------------------- begin

def begin(slug, agent, task, tokens=None, mode="smart"):
    from . import context as ctxmod
    core.ensure_root()
    p = project_dir(slug)
    if not (p / "project.json").exists():
        core.die(f"Unknown project: {slug}")
    cfg = config()
    session_id = f"{stamp()}-{agent}-{uuid.uuid4().hex[:6]}"
    budget = tokens or cfg["default_context_tokens"]
    live = core.repo_state_for(slug)
    rec = reconcile.reconcile(slug, live)
    pending_txn = txn.inspect(slug)
    ctx = ctxmod.build_context(slug, task, budget, mode, reconciliation=rec, live_repo=live, pending_txn=pending_txn)
    ctx_path = ctxmod.write_context(slug, ctx, f"SESSION-{session_id}.md")
    recon_path = ctxmod.write_context(slug, reconcile.render(rec), f"RECONCILE-{session_id}.md")
    cur = p / "CURRENT.md"
    man = core.project_manifest(slug)
    state = {
        "id": session_id, "project": slug, "agent": agent, "task": core.redact(task, 1000), "started_at": iso(),
        "status": "OPEN",
        "lease": {"state": "ACTIVE", "last_heartbeat": iso(), "heartbeats": 0,
                  "stale_after_s": cfg["heartbeat_stale_s"], "abandoned_after_s": cfg["heartbeat_abandoned_s"]},
        "start_current_sha256": sha256_file(cur) if cur.exists() else None,
        "start_next_sha256": sha256_file(p / "NEXT.md") if (p / "NEXT.md").exists() else None,
        "start_memory_version": int(man.get("memory_version", 0)),
        "start_checkpoint": man.get("current_checkpoint"),
        "start_repo_state": live,
        "reconciliation": {"status": rec["status"], "flags": rec["flags"]},
        "context_path": str(ctx_path), "reconcile_path": str(recon_path),
        "context_approx_tokens": core.approx_tokens(ctx),
    }
    sessions_dir(slug).mkdir(exist_ok=True)
    atomic_write_json(session_file(slug, session_id), state)
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "session_begin", "session_id": session_id, "agent": agent,
                                       "task": state["task"], "reconciliation": rec["status"], "flags": rec["flags"]})
    provenance.record_physical_git(slug, live, session=session_id, source_ref="begin")
    out = [f"SESSION_ID={session_id}", f"CONTEXT={ctx_path}", f"APPROX_TOKENS={state['context_approx_tokens']}",
           f"MEMORY_VERSION={state['start_memory_version']}", f"RECONCILIATION={rec['status']}"]
    if rec["flags"]:
        out.append("RECONCILIATION_FLAGS=" + ",".join(rec["flags"]))
    if reconcile.needs_attention(rec):
        out.append(f"RECONCILE_CONTEXT={recon_path}")
    if state["context_approx_tokens"] > cfg.get("context_size_warning_tokens", 9000):
        out.append("WARN=CONTEXT_SIZE_ABOVE_WARNING_THRESHOLD")
    others = [j.get("id") for f, j in open_sessions(slug) if j.get("id") != session_id]
    if others:
        out.append("WARN=OTHER_OPEN_SESSION_EXISTS " + ",".join(others))
    if pending_txn:
        out.append(f"WARN=UNRESOLVED_TRANSACTIONS={len(pending_txn)} (run: aimem txn {slug})")
    return state, out


# ---------------------------------------------------------------- checkpoint

def create_checkpoint(slug, label, result, summary="", session_id=None, advance_current=True, expect_version=None):
    p = project_dir(slug)
    cur, nxt = p / "CURRENT.md", p / "NEXT.md"
    if not cur.exists():
        core.die(f"{slug}: CURRENT.md missing")
    safe_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:90] or "checkpoint"
    base_id = f"{stamp()}__{safe_label}"
    cp_id = base_id
    for n in range(2, 1000):  # unique even when concurrent checkpoints share a second and a label
        cpd = p / "checkpoints" / cp_id
        try:
            cpd.mkdir(parents=True)
            break
        except FileExistsError:
            cp_id = f"{base_id}-{n}"
    else:
        core.die("could not allocate a unique checkpoint id")
    shutil.copy2(cur, cpd / "CURRENT.md")
    if nxt.exists():
        shutil.copy2(nxt, cpd / "NEXT.md")
    repo_state = core.repo_state_for(slug)
    atomic_write_json(cpd / "REPO_STATE.json", repo_state)
    meta = {
        "id": cp_id, "time": iso(), "label": label, "result": result, "summary": core.redact(summary, 4000),
        "session_id": session_id, "advances_current_checkpoint": bool(advance_current),
        "current_sha256": sha256_file(cur), "next_sha256": sha256_file(nxt) if nxt.exists() else None,
        "repo_state": repo_state, "engine_version": core.VERSION,
    }
    atomic_write_json(cpd / "meta.json", meta)
    patch = {"current_checkpoint": cp_id} if advance_current else {}
    version, txid = txn.canonical_update(
        slug, {p / "REPO_STATE.json": core.dumps(repo_state)}, session=session_id, expect_version=expect_version,
        kind="checkpoint", manifest_patch=patch)
    meta["memory_version"] = version
    meta["txn"] = txid
    atomic_write_json(cpd / "meta.json", meta)
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "checkpoint", **meta})
    provenance.record_fact(slug, "checkpoint.current" if advance_current else "checkpoint.admin", cp_id, "CHECKPOINT",
                           source_ref=str(cpd / "meta.json"), status="VERIFIED", session=session_id,
                           evidence_sha256=meta["current_sha256"], note=f"result={result}")
    provenance.record_physical_git(slug, repo_state, session=session_id, source_ref="checkpoint")
    core.rebuild_master_index()
    core.git_memory_commit(f"{slug}: checkpoint {label} [{result}]")
    # Optional navigation publication runs only after canonical local persistence.
    try:
        from . import handoff
        handoff.after_checkpoint(slug)
    except (Exception, SystemExit):
        print("HANDOFF_SYNC=FAILED RETRY=aimem_handoff_--retry-pending")
    return cp_id, version


def finish(slug, session=None, result="", label=None, summary="", allow_unchanged=False,
           no_advance_current=False, expect_version=None, acknowledge_newer=False):
    from . import index as indexmod
    core.ensure_root()
    p = project_dir(slug)
    if session:
        sf = session_file(slug, session)
        if not sf.exists():
            core.die(f"Session not found: {session}")
        state = load_json(sf, {}) or {}
        if state.get("status") != "OPEN":
            core.die(f"Session is not OPEN: {session}")
    else:
        sid, _ = resolve_session(slug, None)
        if not sid:
            core.die(f"No open session for {slug}")
        sf = session_file(slug, sid)
        state = load_json(sf, {}) or {}
    cur = p / "CURRENT.md"
    end_sha = sha256_file(cur) if cur.exists() else None
    changed = end_sha != state.get("start_current_sha256")
    if not changed and not allow_unchanged:
        core.die("CURRENT.md did not change. Update canonical CURRENT.md, or use --allow-unchanged for a verified read-only/no-state-change phase.")
    # Compare-and-swap against concurrent writers
    man = core.project_manifest(slug)
    actual_version = int(man.get("memory_version", 0))
    start_version = int(state.get("start_memory_version", actual_version))
    if expect_version is None:
        if actual_version != start_version and man.get("last_writer_session") not in (None, state.get("id")) and not acknowledge_newer:
            core.die(
                f"CANONICAL_STATE_ADVANCED_BY_ANOTHER_SESSION: memory_version {start_version} -> {actual_version} "
                f"(last writer {man.get('last_writer_session')}). Re-read CURRENT.md/NEXT.md, merge, then finish with "
                f"--expect-version {actual_version}.", core.EXIT_CONFLICT)
        expect_version = actual_version
    lbl = label or state.get("task") or "session finish"
    cp_id, version = create_checkpoint(slug, lbl, result, summary, state.get("id"), not no_advance_current, expect_version)
    state["status"] = "CLOSED"
    state["finished_at"] = iso()
    state["result"] = result
    state["summary"] = core.redact(summary, 4000)
    state["checkpoint"] = cp_id
    state["end_current_sha256"] = end_sha
    state["end_memory_version"] = version
    state["end_repo_state"] = core.repo_state_for(slug)
    state.setdefault("lease", {})["state"] = "CLOSED"
    atomic_write_json(sf, state)
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "session_finish", "session_id": state.get("id"),
                                       "result": result, "checkpoint": cp_id, "memory_version": version})
    indexmod.reindex(slug, quiet=True)
    return cp_id, version


def close_session(slug, sid, result="ABANDONED", note=""):
    """Close an OPEN session WITHOUT a checkpoint (administrative; no canonical change)."""
    sf = session_file(slug, sid)
    state = load_json(sf, {}) or {}
    if state.get("status") != "OPEN":
        core.die(f"Session is not OPEN: {sid}")
    state["status"] = "CLOSED"
    state["finished_at"] = iso()
    state["result"] = result
    state["closed_administratively"] = True
    state["close_note"] = core.redact(note, 1000)
    state.setdefault("lease", {})["state"] = "CLOSED"
    atomic_write_json(sf, state)
    append_jsonl(project_dir(slug) / "EVENTS.jsonl", {"time": iso(), "kind": "session_closed_admin", "session_id": sid,
                                                       "result": result, "note": state["close_note"]})
    return state


def session_summary(slug, f, j, cfg=None):
    st = lease_state(j, cfg)
    last = last_step(slug, j.get("id"))
    return {
        "id": j.get("id"), "agent": j.get("agent"), "task": (j.get("task") or "")[:120], "status": j.get("status"),
        "lease": st, "started_at": j.get("started_at"), "last_heartbeat": (j.get("lease") or {}).get("last_heartbeat"),
        "finished_at": j.get("finished_at"), "result": j.get("result"), "checkpoint": j.get("checkpoint"),
        "last_step": (last or {}).get("summary"), "last_step_time": (last or {}).get("time"),
    }
