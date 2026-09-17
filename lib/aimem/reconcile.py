"""Stale-memory detection: canonical memory vs fresh physical state.

Classes:
  MEMORY_MATCH                  saved head == live head, clean tree
  PHYSICAL_AHEAD                live head descends from saved head (work happened after memory)
  MEMORY_AHEAD_OR_UNVERIFIED    saved head is not reachable from live head (rewound / unknown)
  DIVERGED                      neither is an ancestor of the other
  DIRTY_REPO                    uncommitted changes present (reported alongside a head class)
  RUNTIME_VERIFICATION_REQUIRED no git authority available (no repo / not git); runtime must be verified
CURRENT.md is never rewritten from git alone.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import core
from .core import project_dir, try_load_json

HEX40 = re.compile(r"\b[0-9a-f]{40}\b")


def _is_ancestor(repo, a, b):
    rc, _, _ = core.git_cmd(repo, ["merge-base", "--is-ancestor", a, b])
    return rc == 0


def _commit_exists(repo, h):
    rc, out, _ = core.git_cmd(repo, ["cat-file", "-t", h])
    return rc == 0 and out == "commit"


def _current_md_heads(slug):
    """HEAD hashes mentioned in CURRENT.md (memory's own claims)."""
    p = project_dir(slug) / "CURRENT.md"
    if not p.exists():
        return []
    return list(dict.fromkeys(HEX40.findall(p.read_text(errors="ignore"))))


def reconcile(slug, live=None):
    p = project_dir(slug)
    man = core.project_manifest(slug)
    saved = try_load_json(p / "REPO_STATE.json", {}) or {}
    live = live or core.repo_state_for(slug)
    result = {
        "project": slug, "time": core.iso(), "saved_head": saved.get("head"), "saved_branch": saved.get("branch"),
        "saved_captured_at": saved.get("captured_at"), "live_head": live.get("head"), "live_branch": live.get("branch"),
        "live_dirty": live.get("dirty"), "status": None, "flags": [], "detail": [], "current_md_heads": _current_md_heads(slug),
        "current_checkpoint": man.get("current_checkpoint"),
    }
    if not live.get("configured"):
        result["status"] = "RUNTIME_VERIFICATION_REQUIRED"
        result["detail"].append("No repository configured; physical/runtime state must be verified manually.")
        return result
    if not live.get("exists"):
        result["status"] = "RUNTIME_VERIFICATION_REQUIRED"
        result["flags"].append("REPO_PATH_MISSING")
        result["detail"].append(f"Configured repo path missing: {live.get('path')}")
        return result
    if not live.get("is_git_repo"):
        result["status"] = "RUNTIME_VERIFICATION_REQUIRED"
        result["detail"].append("Repo path is not a git work tree; no git authority.")
        return result
    repo = Path(live["path"])
    sh, lh = saved.get("head"), live.get("head")
    if live.get("dirty"):
        result["flags"].append("DIRTY_REPO")
        result["detail"].append(f"{len(live.get('status_lines') or [])} uncommitted path(s); verify before relying on memory.")
    if not sh:
        result["status"] = "MEMORY_AHEAD_OR_UNVERIFIED"
        result["detail"].append("Memory has no captured repo head; physical state unverified against memory.")
    elif sh == lh:
        result["status"] = "MEMORY_MATCH"
    elif not _commit_exists(repo, sh):
        result["status"] = "MEMORY_AHEAD_OR_UNVERIFIED"
        result["detail"].append(f"Saved head {sh[:12]} is not present in the live repository.")
    elif _is_ancestor(repo, sh, lh):
        result["status"] = "PHYSICAL_AHEAD"
        rc, out, _ = core.git_cmd(repo, ["rev-list", "--count", f"{sh}..{lh}"])
        rc2, files, _ = core.git_cmd(repo, ["diff", "--name-only", sh, lh])
        result["commits_ahead"] = int(out) if rc == 0 and out.isdigit() else None
        result["changed_files"] = files.splitlines()[:60] if rc2 == 0 else []
        result["detail"].append(f"Repository advanced {result.get('commits_ahead')} commit(s) past memory ({sh[:12]} -> {lh[:12]}).")
    elif _is_ancestor(repo, lh, sh):
        result["status"] = "MEMORY_AHEAD_OR_UNVERIFIED"
        result["detail"].append(f"Live head {lh[:12]} is behind memory's saved head {sh[:12]} (rewind/checkout).")
    else:
        result["status"] = "DIVERGED"
        result["detail"].append(f"Saved head {sh[:12]} and live head {lh[:12]} have diverged.")
    if result["status"] == "MEMORY_MATCH" and "DIRTY_REPO" in result["flags"]:
        result["detail"].append("Committed state matches memory but the work tree is dirty.")
    # CURRENT.md claims vs live
    heads = result["current_md_heads"]
    if lh and heads and lh not in heads:
        result["flags"].append("CURRENT_MD_HEAD_MISMATCH")
        result["detail"].append(f"CURRENT.md mentions HEAD(s) {', '.join(h[:12] for h in heads[:3])} but live HEAD is {lh[:12]}.")
    return result


def needs_attention(r):
    return r.get("status") != "MEMORY_MATCH" or bool(r.get("flags"))


def render(r):
    lines = [
        "# MEMORY / PHYSICAL RECONCILIATION — GENERATED, NOT CANONICAL",
        f"PROJECT: {r['project']}", f"GENERATED: {r['time']}", f"STATUS: {r['status']}",
        f"FLAGS: {', '.join(r['flags']) or 'none'}",
        f"SAVED_HEAD: {r.get('saved_head')} ({r.get('saved_branch')}) captured {r.get('saved_captured_at')}",
        f"LIVE_HEAD: {r.get('live_head')} ({r.get('live_branch')}) dirty={r.get('live_dirty')}",
        f"CURRENT_CHECKPOINT: {r.get('current_checkpoint')}",
    ]
    if r.get("commits_ahead") is not None:
        lines.append(f"COMMITS_AHEAD: {r['commits_ahead']}")
    if r.get("changed_files"):
        lines.append("CHANGED_FILES_SINCE_MEMORY:")
        lines += [f"  - {f}" for f in r["changed_files"][:40]]
    if r.get("detail"):
        lines.append("DETAIL:")
        lines += [f"  - {d}" for d in r["detail"]]
    lines += ["", "RULES:",
              "- Freshly verified physical state overrides stale memory.",
              "- CURRENT.md is NOT rewritten automatically from git; runtime/database/external state may differ.",
              "- Verify, then update CURRENT.md/NEXT.md and finish with a truthful result."]
    return "\n".join(lines) + "\n"
