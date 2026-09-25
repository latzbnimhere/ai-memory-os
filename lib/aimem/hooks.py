"""Claude Code hooks: automatic session begin, step logging, heartbeat and close.

Claude Code runs `aimem hooks run` on SessionStart, UserPromptSubmit, PostToolUse,
PostToolUseFailure, Stop, PreCompact and SessionEnd, passing one JSON object on stdin.
The runner binds each Claude Code session id to one aimem session per registered project
(binding files under .run/hooks/claude/, disposable). It never blocks Claude Code: every
failure goes to stderr and the exit code is always 0. Only SessionStart writes to stdout
(a bounded context pack, which Claude Code adds to the agent's context).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import core
from .core import ROOT, RUN, atomic_write, atomic_write_json, config, iso, stamp, try_load_json

AGENT = "claude"
EVENTS = ("SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "PreCompact", "SessionEnd")
WRITE_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
TOOL_MATCHER = "^(Edit|MultiEdit|Write|NotebookEdit|Bash)$"
TEST_RX = re.compile(r"\b(pytest|unittest|npm test|pnpm test|yarn test|go test|cargo test|make test|jest|vitest|mocha|rspec|phpunit|tox|ctest)\b")
PLACEHOLDER_TASK = "claude-code session"
STDOUT_CAP = 10_000  # Claude Code caps plain hook stdout at 10,000 characters
MEANINGFUL_KINDS = {"write", "command", "test", "error", "decision", "result", "checkpoint"}


# ---------------------------------------------------------------- bindings (Claude Code session id -> aimem session)

def binding_file(claude_sid):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(claude_sid))[:80] or "unknown"
    return RUN / "hooks" / "claude" / f"{safe}.json"


def load_binding(claude_sid, slug):
    b = try_load_json(binding_file(claude_sid), None)
    if not isinstance(b, dict) or b.get("project") != slug or not b.get("session_id"):
        return None
    return b


def save_binding(claude_sid, slug, session_id, cwd):
    atomic_write_json(binding_file(claude_sid), {"claude_session_id": str(claude_sid), "project": slug, "session_id": session_id,
                                                 "cwd": str(cwd), "bound_at": iso()})


def bound_session(slug, claude_sid):
    """The OPEN aimem session bound to this Claude Code session, else None."""
    from . import sessions
    b = load_binding(claude_sid, slug)
    if not b:
        return None
    j = try_load_json(sessions.session_file(slug, b["session_id"]), None)
    if not isinstance(j, dict) or j.get("status") != "OPEN":
        return None
    return b["session_id"]


def open_session(slug, claude_sid, cwd, source, tokens=None):
    from . import sessions
    state, out = sessions.begin(slug, AGENT, f"{PLACEHOLDER_TASK} ({source})", tokens, "smart")
    save_binding(claude_sid, slug, state["id"], cwd)
    return state, out


def ensure_session(slug, claude_sid, cwd, source):
    sid = bound_session(slug, claude_sid)
    if sid:
        return sid
    state, _ = open_session(slug, claude_sid, cwd, source)
    return state["id"]


def context_budget():
    cfg = config()
    return int(cfg.get("hook_context_tokens") or cfg["default_context_tokens"])


# ---------------------------------------------------------------- event handlers

def previous_unfinished(slug, exclude):
    """Id of the most recently closed session if the hook closed it without a checkpoint."""
    from . import sessions
    latest = None
    for _f, j in sessions.all_sessions(slug):
        if j.get("id") == exclude or j.get("status") != "CLOSED":
            continue
        if latest is None or (j.get("finished_at") or "") > (latest.get("finished_at") or ""):
            latest = j
    if latest and latest.get("result") == "UNFINISHED":
        return latest.get("id")
    return None


def footer(slug, session_id, rec_status, recon_path=None, warns=(), unfinished=None, full_context=None):
    root = str(ROOT)
    lines = ["", "---", "## AI MEMORY OS HOOKS", "AIMEM_HOOKS=ACTIVE", f"AIMEM_PROJECT={slug}", f"AIMEM_SESSION_ID={session_id}",
             f"AIMEM_ROOT={root}", f"RECONCILIATION={rec_status}"]
    if recon_path:
        lines.append(f"RECONCILE_CONTEXT={recon_path}")
    lines += [f"WARN={w}" for w in warns]
    if unfinished:
        lines.append(f"PREVIOUS_SESSION_UNFINISHED={unfinished} (no checkpoint; steps: {root}/projects/{slug}/sessions/{unfinished}.steps.jsonl)")
    if full_context:
        lines.append(f"FULL_CONTEXT={full_context} (this pack was shortened to fit the hook output cap)")
    lines += [
        "Claude Code hooks opened this AI Memory session and log prompts, file writes, commands and tool errors automatically.",
        "Do not run `aimem begin` again. For manual steps or durable notes, bind to this session explicitly:",
        f"  {root}/bin/aimem-step --project {slug} --session {session_id} --kind <kind> --summary \"<compact fact>\"",
        f"  {root}/bin/aimem note {slug} --kind decision|error --text \"...\" --session {session_id}",
        f"At closeout: update {root}/projects/{slug}/CURRENT.md and NEXT.md compactly, then finish truthfully:",
        f"  {root}/bin/aimem finish {slug} --session {session_id} --result <PASS|STOP|PARTIAL> --label \"<checkpoint>\"",
        "Without a finish, the session is closed as UNFINISHED (no checkpoint) when Claude Code exits.",
        "",
    ]
    return "\n".join(lines)


def fit_stdout(slug, task, ctx, foot_without_full, foot_with_full):
    """Return context+footer within Claude Code's plain-stdout cap; rebuild a smaller pack when needed."""
    from . import context as ctxmod
    from . import reconcile, txn
    if len(ctx) + len(foot_without_full) <= STDOUT_CAP:
        return ctx + foot_without_full
    room = STDOUT_CAP - len(foot_with_full) - 80
    live = core.repo_state_for(slug)
    rec = reconcile.reconcile(slug, live)
    small = ctxmod.build_context(slug, task, int(room / 3.2), "smart", reconciliation=rec, live_repo=live, pending_txn=txn.inspect(slug))
    if len(small) > room:
        small = small[:max(0, room - 40)] + "\n[TRUNCATED_FOR_HOOK_STDOUT_CAP]\n"
    return small + foot_with_full


def on_session_start(slug, claude_sid, cwd, payload):
    from . import context as ctxmod
    from . import reconcile, sessions, txn
    source = str(payload.get("source") or "startup")
    warns = []
    existing = bound_session(slug, claude_sid)
    if existing:
        # resume / compact: same Claude Code session, re-inject a fresh bounded pack
        state = sessions.load_session(slug, existing)
        sessions.touch_heartbeat(slug, existing, note=f"claude-code {source}")
        live = core.repo_state_for(slug)
        rec = reconcile.reconcile(slug, live)
        task = state.get("task") or ""
        ctx = ctxmod.build_context(slug, task, context_budget(), "smart", reconciliation=rec, live_repo=live, pending_txn=txn.inspect(slug))
        ctx_path = ctxmod.write_context(slug, ctx, f"SESSION-{existing}.md")
        session_id, rec_status = existing, rec["status"]
        recon_path = state.get("reconcile_path") if reconcile.needs_attention(rec) else None
    else:
        state, out = open_session(slug, claude_sid, cwd, source, context_budget())
        kv = dict(line.split("=", 1) for line in out if "=" in line and not line.startswith("WARN="))
        warns = [line[len("WARN="):] for line in out if line.startswith("WARN=")]
        session_id, task = state["id"], state.get("task") or ""
        ctx_path = Path(kv["CONTEXT"])
        ctx = ctx_path.read_text(errors="ignore")
        rec_status, recon_path = kv.get("RECONCILIATION"), kv.get("RECONCILE_CONTEXT")
    unfinished = previous_unfinished(slug, session_id)
    plain = footer(slug, session_id, rec_status, recon_path, warns, unfinished)
    with_full = footer(slug, session_id, rec_status, recon_path, warns, unfinished, full_context=ctx_path)
    return fit_stdout(slug, task, ctx, plain, with_full)


def on_user_prompt(slug, claude_sid, cwd, payload):
    from . import sessions, step
    prompt = " ".join(str(payload.get("prompt") or "").split())
    if not prompt:
        return ""
    session_id = ensure_session(slug, claude_sid, cwd, "late-bind")
    if not config().get("hook_log_prompts", True):
        sessions.touch_heartbeat(slug, session_id, note="claude-code prompt")
        return ""
    summary = prompt[:240]
    sf = sessions.session_file(slug, session_id)
    j = sessions.load_session(slug, session_id)
    if (j.get("task") or "").startswith(PLACEHOLDER_TASK):
        j["task"] = core.redact(summary, 1000)
        atomic_write_json(sf, j)
    step.record_step(slug, session_id, "hook", AGENT, "plan", f"prompt: {summary}", cwd=cwd)
    return ""


def display_path(path, cwd):
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(Path(cwd).resolve()))
    except Exception:
        return str(path)


def classify_tool(tool, tin, cwd):
    """(kind, summary, command, files) for a tool call worth a step, else None."""
    if tool in WRITE_TOOLS:
        path = str(tin.get("file_path") or tin.get("notebook_path") or "")
        return "write", f"{tool} {display_path(path, cwd)}".strip(), None, [path] if path else []
    if tool == "Bash":
        command = str(tin.get("command") or "").strip()
        if not command:
            return None
        desc = " ".join(str(tin.get("description") or "").split())
        first = " ".join(command.split())[:160]
        kind = "test" if TEST_RX.search(command) else "command"
        return kind, (desc or first)[:200], command[:600], []
    return None


def tool_result(tres):
    """(result, exit_code) derived defensively from a tool_response of unknown shape."""
    if not isinstance(tres, dict):
        return None, None
    if tres.get("interrupted"):
        return "INTERRUPTED", None
    for key in ("exit_code", "exitCode", "returncode", "code"):
        v = tres.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return ("OK" if v == 0 else f"EXIT={v}"), v
    if tres.get("is_error") or tres.get("error"):
        return "ERROR", None
    return None, None


def on_post_tool_use(slug, claude_sid, cwd, payload):
    from . import step
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    classified = classify_tool(tool, tin, cwd)
    if not classified:
        return ""
    kind, summary, command, files = classified
    result, exit_code = tool_result(payload.get("tool_response"))
    session_id = ensure_session(slug, claude_sid, cwd, "late-bind")
    step.record_step(slug, session_id, "hook", AGENT, kind, summary, result=result, command=command, files=files,
                     exit_code=exit_code, cwd=cwd)
    return ""


def on_post_tool_use_failure(slug, claude_sid, cwd, payload):
    from . import step
    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    error = " ".join(str(payload.get("error") or "").split())[:300]
    classified = classify_tool(tool, tin, cwd)
    summary, command, files = (classified[1], classified[2], classified[3]) if classified else (tool, None, [])
    session_id = ensure_session(slug, claude_sid, cwd, "late-bind")
    step.record_step(slug, session_id, "hook", AGENT, "error", f"{summary} failed: {error}", result="ERROR", command=command,
                     files=files, cwd=cwd)
    return ""


def on_stop(slug, claude_sid, cwd, payload):
    from . import sessions
    sid = bound_session(slug, claude_sid)
    if sid:
        sessions.touch_heartbeat(slug, sid, note="claude-code turn ended")
    return ""


def on_pre_compact(slug, claude_sid, cwd, payload):
    from . import step
    sid = bound_session(slug, claude_sid)
    if sid:
        step.record_step(slug, sid, "hook", AGENT, "other", f"claude-code context compaction ({payload.get('trigger') or 'auto'})", cwd=cwd)
    return ""


def on_session_end(slug, claude_sid, cwd, payload):
    from . import sessions
    b = load_binding(claude_sid, slug)
    if not b:
        return ""
    sid = b["session_id"]
    j = try_load_json(sessions.session_file(slug, sid), None)
    if isinstance(j, dict) and j.get("status") == "OPEN":
        steps = core.read_jsonl(sessions.steps_file(slug, sid))
        meaningful = any(s.get("step_kind") in MEANINGFUL_KINDS for s in steps)
        result = "UNFINISHED" if meaningful else "EMPTY"
        sessions.close_session(slug, sid, result, note=f"claude-code session ended ({payload.get('reason') or 'other'}); closed by hook without a checkpoint")
    binding_file(claude_sid).unlink(missing_ok=True)
    return ""


HANDLERS = {
    "SessionStart": on_session_start, "UserPromptSubmit": on_user_prompt, "PostToolUse": on_post_tool_use,
    "PostToolUseFailure": on_post_tool_use_failure, "Stop": on_stop, "PreCompact": on_pre_compact, "SessionEnd": on_session_end,
}


def dispatch(event, payload):
    """Route one hook payload; returns the text for stdout (non-empty only for SessionStart)."""
    handler = HANDLERS.get(event)
    if not handler or not core.REGISTRY.exists():
        return ""
    claude_sid = str(payload.get("session_id") or "")
    cwd = str(payload.get("cwd") or os.getcwd())
    slug = core.detect_project(cwd)
    if not claude_sid or not slug or not core.project_exists(slug):
        return ""
    core.ensure_root()
    return handler(slug, claude_sid, cwd, payload) or ""


def run_stdin(event_override=None):
    """Entry point for Claude Code. Fail open: never raise, never exit non-zero, never block."""
    if os.environ.get("AIMEM_HOOKS_DISABLE"):
        return 0
    event = event_override or "?"
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        event = event_override or str(payload.get("hook_event_name") or "")
        out = dispatch(event, payload)
        if out:
            sys.stdout.write(out)
            sys.stdout.flush()
    except (Exception, SystemExit) as e:  # a memory problem must never break the agent session
        print(f"AIMEM_HOOK=SKIPPED event={event} reason={type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
    return 0


# ---------------------------------------------------------------- settings.json management

def default_target():
    return Path.home() / ".claude" / "settings.json"


def hook_command():
    return f'AI_MEMORY_ROOT="{ROOT}" "{ROOT}/bin/aimem" hooks run'


def hook_entries():
    cmd = hook_command()

    def h(timeout):
        return {"type": "command", "command": cmd, "timeout": timeout}
    return {
        "SessionStart": [{"hooks": [h(30)]}],
        "UserPromptSubmit": [{"hooks": [h(15)]}],
        "PostToolUse": [{"matcher": TOOL_MATCHER, "hooks": [h(15)]}],
        "PostToolUseFailure": [{"matcher": TOOL_MATCHER, "hooks": [h(15)]}],
        "Stop": [{"hooks": [h(15)]}],
        "PreCompact": [{"hooks": [h(15)]}],
        "SessionEnd": [{"hooks": [h(10)]}],  # SessionEnd hooks share a 1.5s budget unless a timeout is set
    }


def is_ours(h):
    cmd = str((h or {}).get("command") or "") if isinstance(h, dict) else ""
    return "aimem" in cmd and cmd.rstrip().endswith("hooks run")


def merge(settings, install=True):
    """Replace every aimem hook entry with the current set (or remove all of them). Foreign hooks are preserved."""
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    ours = hook_entries()
    for event in EVENTS:
        kept = []
        for entry in hooks.get(event) or []:
            if isinstance(entry, dict) and isinstance(entry.get("hooks"), list):
                inner = [x for x in entry["hooks"] if not is_ours(x)]
                if entry["hooks"] and not inner:
                    continue  # this entry was entirely ours
                entry = dict(entry, hooks=inner)
            kept.append(entry)
        if install:
            kept += ours[event]
        if kept:
            hooks[event] = kept
        else:
            hooks.pop(event, None)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return settings


def load_settings(target: Path):
    if not target.exists():
        return None
    try:
        settings = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:
        core.die(f"{target} is not valid JSON ({e}); refusing to modify it")
    if not isinstance(settings, dict):
        core.die(f"{target} is not a JSON object; refusing to modify it")
    return settings


def apply(target, install, dry_run=False):
    target = Path(target).expanduser()
    current = load_settings(target)
    new = merge(json.loads(json.dumps(current if current is not None else {})), install=install)
    if current is not None and current == new:
        return "UNCHANGED", None
    if current is None and not install:
        return "UNCHANGED", None
    if dry_run:
        return "DRY_RUN", None
    backup = None
    if current is not None:
        backup = target.with_name(target.name + f".pre-aimem-hooks-{stamp()}.bak")
        shutil.copy2(target, backup)
    atomic_write(target, json.dumps(new, indent=2, ensure_ascii=False) + "\n", validate=core.validate_json_file)
    return ("CREATED" if current is None else "UPDATED"), backup


def status(target):
    """(state, installed_events, installed_commands) for a settings file."""
    target = Path(target).expanduser()
    settings = try_load_json(target, None)
    hooks = settings.get("hooks") if isinstance(settings, dict) and isinstance(settings.get("hooks"), dict) else {}
    installed, commands = [], set()
    for event in EVENTS:
        for entry in hooks.get(event) or []:
            if isinstance(entry, dict):
                mine = [x for x in (entry.get("hooks") or []) if is_ours(x)]
                if mine:
                    installed.append(event)
                    commands.update(x["command"] for x in mine)
                    break
    state = "INSTALLED" if len(installed) == len(EVENTS) else ("PARTIAL" if installed else "NOT_INSTALLED")
    return state, installed, sorted(commands)


def render_snippet():
    return json.dumps({"hooks": hook_entries()}, indent=2) + "\n"
