"""Structured provenance for authoritative facts.

Machine-readable, append-only: projects/<slug>/PROVENANCE.jsonl
CURRENT.md stays human-readable; this store carries source/verification metadata.

Fact record:
  {fact_id, time, key, value, source_type, source_ref, verified_at, session,
   evidence_sha256, status, note}

source_type in SOURCE_TYPES; status in STATUSES.
The "view" (latest fact per key) is derived; STALE is computed at read time when a
PHYSICAL_GIT fact no longer matches fresh repo state.
"""
from __future__ import annotations

import uuid

from . import core
from .core import append_jsonl, iso, project_dir, read_jsonl

SOURCE_TYPES = [
    "OWNER_INSTRUCTION", "PHYSICAL_GIT", "PHYSICAL_RUNTIME", "DATABASE_QUERY", "LOCAL_FILE",
    "CHECKPOINT", "CLAUDE_SESSION", "CODEX_SESSION", "CHATGPT_HANDOFF", "DERIVED", "UNVERIFIED",
]
STATUSES = ["VERIFIED", "REPORTED", "INFERRED", "UNCONFIRMED", "STALE"]

PHYSICAL_KEYS = ("repo.head", "repo.branch", "repo.dirty", "repo.tree")


def prov_path(slug):
    return project_dir(slug) / "PROVENANCE.jsonl"


def record_fact(slug, key, value, source_type, source_ref="", status="REPORTED", session=None,
                evidence_sha256=None, note="", verified_at=None):
    if source_type not in SOURCE_TYPES:
        core.die(f"Unknown source_type {source_type}; allowed: {', '.join(SOURCE_TYPES)}")
    if status not in STATUSES:
        core.die(f"Unknown status {status}; allowed: {', '.join(STATUSES)}")
    rec = {
        "fact_id": uuid.uuid4().hex[:12], "time": iso(), "key": core.redact(key, 200),
        "value": core.redact(value, 2000), "source_type": source_type,
        "source_ref": core.redact(source_ref, 500), "verified_at": verified_at or (iso() if status == "VERIFIED" else None),
        "session": session, "evidence_sha256": evidence_sha256, "status": status, "note": core.redact(note, 1000),
    }
    append_jsonl(prov_path(slug), rec)
    return rec


def record_physical_git(slug, repo_state, session=None, source_ref="git"):
    """Record fresh git facts as VERIFIED PHYSICAL_GIT."""
    if not repo_state or not repo_state.get("is_git_repo"):
        return []
    out = []
    for key, val in [("repo.head", repo_state.get("head")), ("repo.branch", repo_state.get("branch")),
                     ("repo.dirty", "YES" if repo_state.get("dirty") else "NO"), ("repo.tree", repo_state.get("tree"))]:
        if val is None:
            continue
        out.append(record_fact(slug, key, val, "PHYSICAL_GIT", source_ref=f"{source_ref}:{repo_state.get('path')}",
                               status="VERIFIED", session=session, verified_at=repo_state.get("captured_at")))
    return out


def all_facts(slug):
    return read_jsonl(prov_path(slug))


def latest_view(slug, fresh_repo_state=None):
    """Latest fact per key; PHYSICAL_GIT facts marked STALE if they mismatch fresh state."""
    view = {}
    for f in all_facts(slug):
        view[f.get("key")] = f
    if fresh_repo_state and fresh_repo_state.get("is_git_repo"):
        fresh = {"repo.head": fresh_repo_state.get("head"), "repo.branch": fresh_repo_state.get("branch"),
                 "repo.dirty": "YES" if fresh_repo_state.get("dirty") else "NO", "repo.tree": fresh_repo_state.get("tree")}
        for k, cur in fresh.items():
            f = view.get(k)
            if f and f.get("status") != "STALE" and cur is not None and str(f.get("value")) != str(cur):
                f = dict(f)
                f["status"] = "STALE"
                f["stale_reason"] = f"fresh value {cur}"
                view[k] = f
    return view


def warnings(slug, fresh_repo_state=None):
    """Provenance warnings for doctor/health."""
    warns = []
    view = latest_view(slug, fresh_repo_state)
    for k, f in sorted(view.items()):
        if f.get("status") == "STALE":
            warns.append(f"{slug}: fact {k} is STALE (memory={f.get('value')} vs {f.get('stale_reason')})")
        if f.get("source_type") == "CHATGPT_HANDOFF" and f.get("status") in ("REPORTED", "UNCONFIRMED") and k in PHYSICAL_KEYS:
            warns.append(f"{slug}: physical fact {k} only REPORTED by ChatGPT handoff; verify locally")
    return warns


def compact_view_text(slug, fresh_repo_state=None, limit=30):
    view = latest_view(slug, fresh_repo_state)
    if not view:
        return ""
    lines = []
    for k in sorted(view)[:limit]:
        f = view[k]
        v = str(f.get("value"))
        if len(v) > 100:
            v = v[:97] + "..."
        lines.append(f"- {k} = {v}  [{f.get('status')}/{f.get('source_type')} @ {f.get('verified_at') or f.get('time')}]")
    return "\n".join(lines)
