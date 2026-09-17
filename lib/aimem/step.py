"""aimem-step: append a compact operational step, bound to an explicit session; refreshes the lease heartbeat."""
from __future__ import annotations

import argparse
import os

from . import core, sessions
from .core import REGISTRY, append_jsonl, day, iso, project_dir, redact

KINDS = ["plan", "read", "command", "write", "test", "decision", "error", "result", "checkpoint", "other"]


def record_step(slug, session_id, binding, agent, kind, summary, result=None, command=None, files=None, exit_code=None, cwd=None):
    p = project_dir(slug)
    reg = core.try_load_json(REGISTRY, {"projects": {}}) or {"projects": {}}
    man = core.try_load_json(p / "project.json", {}) or {}
    repo = (reg.get("projects", {}).get(slug, {}) or {}).get("repo") or man.get("repo")
    event = {
        "time": iso(), "kind": "operational_step", "step_kind": kind, "project": slug, "agent": redact(agent),
        "session_id": session_id, "session_binding": binding, "summary": redact(summary), "result": redact(result),
        "command": redact(command), "files": [redact(x) for x in (files or [])][:50], "exit_code": exit_code,
        "cwd": redact(cwd or os.getcwd()), "repo_state": core.repo_state_for_path(repo),
        "log_policy": "compact_operational_trace_no_hidden_reasoning_no_raw_secrets",
    }
    with core.project_step_lock(slug):
        append_jsonl(p / "cold" / "step-journal" / f"{day()}.jsonl", event)
        if session_id:
            append_jsonl(sessions.steps_file(slug, session_id), event)
        if kind in {"decision", "error", "result", "checkpoint"}:
            append_jsonl(p / "EVENTS.jsonl", event)
    if session_id:
        try:
            sessions.touch_heartbeat(slug, session_id, note=f"{kind}: {summary[:80]}")
        except SystemExit:
            pass
    return event


def main(argv=None):
    ap = argparse.ArgumentParser(prog="aimem-step", description="Append a compact operational step to AI Memory (V4).")
    ap.add_argument("--project")
    ap.add_argument("--session", help="Explicit AI Memory session id. Required when multiple sessions are open.")
    ap.add_argument("--agent", default="other")
    ap.add_argument("--kind", choices=KINDS, required=True)
    ap.add_argument("--summary", required=True)
    ap.add_argument("--result")
    ap.add_argument("--command")
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--exit-code", type=int)
    ap.add_argument("--cwd", default=os.getcwd())
    args = ap.parse_args(argv)
    if not REGISTRY.exists():
        core.die("AI Memory registry missing.")
    slug = args.project or core.detect_project(args.cwd)
    if not slug:
        core.die("Could not detect registered project; pass --project.")
    if not core.project_exists(slug):
        core.die(f"Unknown project {slug}")
    session_id, binding = sessions.resolve_session(slug, args.session)
    record_step(slug, session_id, binding, args.agent, args.kind, args.summary, args.result, args.command, args.file, args.exit_code, args.cwd)
    print(f"STEP_STORED project={slug} kind={args.kind} session={session_id or '-'} binding={binding}")
