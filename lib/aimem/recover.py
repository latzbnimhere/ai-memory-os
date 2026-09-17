"""Crash / abandoned session detection and bounded recovery packets (read-only by default)."""
from __future__ import annotations

from pathlib import Path

from . import core, sessions, txn
from .core import iso, project_dir, read_jsonl, sha256_file, try_load_json


def assess_session(slug, f, j):
    """Classify an OPEN session. Returns dict with recovery_required flag and reasons."""
    st = sessions.lease_state(j)
    p = project_dir(slug)
    reasons = []
    cur = p / "CURRENT.md"
    cur_sha = sha256_file(cur) if cur.exists() else None
    current_changed = cur_sha != j.get("start_current_sha256")
    live = core.repo_state_for(slug)
    start_repo = j.get("start_repo_state") or {}
    repo_changed = False
    if live.get("is_git_repo"):
        repo_changed = (live.get("head") != start_repo.get("head")) or (bool(live.get("dirty")) != bool(start_repo.get("dirty"))) \
            or (live.get("tree") != start_repo.get("tree"))
    steps = read_jsonl(sessions.steps_file(slug, j.get("id")))
    has_finish = j.get("status") == "CLOSED" and bool(j.get("checkpoint"))
    if j.get("status") == "OPEN" and st in ("STALE", "ABANDONED"):
        reasons.append(f"lease {st}")
        if repo_changed:
            reasons.append("repo changed since session start")
        if current_changed:
            reasons.append("CURRENT.md changed since session start")
        if not has_finish:
            reasons.append("no finish checkpoint")
    recovery_required = j.get("status") == "OPEN" and st in ("STALE", "ABANDONED") and (repo_changed or current_changed or bool(steps))
    return {"id": j.get("id"), "lease": st, "status": j.get("status"), "recovery_required": recovery_required,
            "reasons": reasons, "repo_changed": repo_changed, "current_changed": current_changed, "steps": len(steps),
            "live": live, "start_repo": start_repo}


def scan(slug):
    return [assess_session(slug, f, j) for f, j in sessions.open_sessions(slug)]


def scan_all():
    out = {}
    for slug in core.all_slugs():
        if core.project_exists(slug):
            out[slug] = scan(slug)
    return out


def _changed_files(start_repo, live):
    if not (live.get("is_git_repo") and start_repo.get("head") and live.get("head")):
        return []
    repo = Path(live["path"])
    files = []
    if start_repo.get("head") != live.get("head"):
        rc, out, _ = core.git_cmd(repo, ["diff", "--name-only", start_repo["head"], live["head"]])
        if rc == 0:
            files += out.splitlines()
    files += [l[3:] for l in (live.get("status_lines") or [])]
    return list(dict.fromkeys(files))[:80]


def packet(slug, sid):
    """Bounded recovery context for one session (does not rerun anything)."""
    p = project_dir(slug)
    f = sessions.session_file(slug, sid)
    j = try_load_json(f, None)
    if j is None:
        core.die(f"Session not found: {sid}")
    a = assess_session(slug, f, j)
    steps = read_jsonl(sessions.steps_file(slug, sid))
    live, start = a["live"], a["start_repo"]
    last_result = next((s for s in reversed(steps) if s.get("result")), None)
    last_test = next((s for s in reversed(steps) if s.get("step_kind") == "test"), None)
    last_pass_idx = max([i for i, s in enumerate(steps) if str(s.get("result") or "").upper().startswith("PASS")] or [-1])
    unresolved_errors = [s for s in steps[last_pass_idx + 1:] if s.get("step_kind") == "error"]
    completed = [s for s in steps if s.get("step_kind") in ("write", "command", "checkpoint") and str(s.get("result") or "").upper().startswith("PASS")]
    changed = _changed_files(start, live)
    pend = txn.inspect(slug)
    L = [
        "# RECOVERY CONTEXT — GENERATED, NOT CANONICAL", f"PROJECT: {slug}", f"SESSION: {sid}", f"GENERATED: {iso()}",
        f"RECOVERY_REQUIRED: {'YES' if a['recovery_required'] else 'NO'}", f"LEASE: {a['lease']}  STATUS: {j.get('status')}",
        f"REASONS: {'; '.join(a['reasons']) or '-'}", f"AGENT: {j.get('agent')}", f"TASK: {j.get('task')}",
        f"STARTED: {j.get('started_at')}  LAST_HEARTBEAT: {(j.get('lease') or {}).get('last_heartbeat')}",
        "", "## Last meaningful step",
        (f"- {steps[-1].get('time')} [{steps[-1].get('step_kind')}] {steps[-1].get('summary')} result={steps[-1].get('result')}" if steps else "- (no steps recorded)"),
        "", "## Starting repo state", f"- head={start.get('head')} branch={start.get('branch')} dirty={start.get('dirty')}",
        "## Current repo state", f"- head={live.get('head')} branch={live.get('branch')} dirty={live.get('dirty')}",
        "", "## Changed files (since session start; git diff + working tree)"] + ([f"- {x}" for x in changed] or ["- none detected"]) + [
        "", "## Test state (last recorded)", (f"- {last_test.get('time')} {last_test.get('summary')} => {last_test.get('result')}" if last_test else "- unknown (no test step recorded)"),
        "## Last verified result", (f"- {last_result.get('time')} [{last_result.get('step_kind')}] {last_result.get('summary')} => {last_result.get('result')}" if last_result else "- none"),
        "", "## Unresolved errors (after last PASS)"] + ([f"- {s.get('time')} {s.get('summary')}" for s in unresolved_errors[-10:]] or ["- none recorded"]) + [
        "", "## Must NOT be repeated (already completed with PASS)"] + ([f"- {s.get('time')} [{s.get('step_kind')}] {s.get('summary')}" for s in completed[-15:]] or ["- nothing recorded as completed"]) + [
        "", "## Unresolved transactions"] + ([f"- {t['id']} state={t['state']} resolution={t['resolution']}" for t in pend] or ["- none"]) + [
        "", "## Recovery rules",
        "- Nothing is rerun automatically. Verify physical state first; then either continue under a NEW session or close this one:",
        f"  aimem session close {slug} --session {sid} --result ABANDONED --note '<why>'",
        "- Do not repeat completed PASS steps. Do not trust CURRENT.md over freshly verified state.",
    ]
    text = "\n".join(L) + "\n"
    out = p / ".generated" / f"RECOVERY-{sid}.md"
    core.atomic_write(out, text)
    return a, text, out
