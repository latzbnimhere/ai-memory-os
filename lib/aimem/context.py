"""Bounded context packs: huge durable history, minimum relevant context."""
from __future__ import annotations

import json
from pathlib import Path

from . import core, provenance
from .core import ROOT, approx_tokens, atomic_write, config, iso, project_dir, read_tail


def write_context(slug, text, name="CONTEXT.md"):
    p = project_dir(slug) / ".generated"
    p.mkdir(exist_ok=True)
    target = p / name
    atomic_write(target, text)
    return target


def latest_summary_text(slug, max_chars=2500):
    d = project_dir(slug) / "cold" / "summaries" / "phase"
    if not d.exists():
        return ""
    files = sorted(d.glob("*.md"))
    if not files:
        return ""
    txt = files[-1].read_text(errors="ignore")
    return txt[:max_chars]


def open_session_lines(slug):
    from . import sessions
    lines = []
    for f, j in sessions.open_sessions(slug):
        st = sessions.lease_state(j)
        lines.append(f"- {j.get('id')} agent={j.get('agent')} lease={st} last_heartbeat={(j.get('lease') or {}).get('last_heartbeat')} task={(j.get('task') or '')[:80]}")
    return "\n".join(lines)


def build_context(slug, query, token_budget, mode="smart", reconciliation=None, live_repo=None, pending_txn=None):
    from . import index as indexmod
    from . import reconcile as reconmod
    p = project_dir(slug)
    man = core.project_manifest(slug)
    cfg = config()
    live_repo = live_repo if live_repo is not None else core.repo_state_for(slug)
    reconciliation = reconciliation or reconmod.reconcile(slug, live_repo)

    requested_budget = max(1200, int(token_budget or cfg["default_context_tokens"]))
    needs_attention = bool(
        reconmod.needs_attention(reconciliation)
        or live_repo.get("dirty")
        or pending_txn
    )

    if mode == "deep" or needs_attention:
        token_budget = requested_budget
    elif mode == "hot":
        token_budget = min(requested_budget, 3000)
    else:
        token_budget = min(
            requested_budget,
            int(cfg.get("context_clean_target_tokens", 3600)),
        )

    sections = []

    def sec(title, text, authority=""):
        if text and text.strip():
            sections.append((title, text.strip(), authority))

    if (ROOT / "global" / "OWNER_RULES.md").exists():
        sec("GLOBAL OWNER RULES", (ROOT / "global" / "OWNER_RULES.md").read_text(), "GLOBAL")
    if (ROOT / "global" / "PREFERENCES.md").exists():
        sec("GLOBAL PREFERENCES", (ROOT / "global" / "PREFERENCES.md").read_text(), "GLOBAL")
    compact_man = {k: man.get(k) for k in ("slug", "name", "repo", "current_checkpoint", "memory_version", "updated_at", "last_writer_session")}
    sec("PROJECT MANIFEST", json.dumps(compact_man, indent=2), "STRUCTURED")
    sec("MEMORY/PHYSICAL RECONCILIATION", reconmod.render(reconciliation), "GENERATED_FRESH")
    if live_repo.get("is_git_repo"):
        sec("FRESH PHYSICAL REPO STATE", json.dumps({k: live_repo.get(k) for k in ("path", "branch", "head", "tree", "dirty", "captured_at")}, indent=2)
            + ("\nSTATUS_LINES:\n" + "\n".join(live_repo.get("status_lines", [])[:25]) if live_repo.get("dirty") else ""), "PHYSICAL_FRESH")
    sec("CURRENT AUTHORITATIVE STATE", (p / "CURRENT.md").read_text(errors="ignore"), "CURRENT")
    sec("NEXT ACTIONS", (p / "NEXT.md").read_text(errors="ignore"), "CURRENT")
    sec("PROVENANCE FACTS (latest per key)", provenance.compact_view_text(slug, live_repo), "STRUCTURED_PROVENANCE")
    sec("OPEN SESSIONS / LEASES", open_session_lines(slug), "LIVE")
    if pending_txn:
        sec("UNRESOLVED TRANSACTIONS", "\n".join(f"- {t['id']} state={t['state']} resolution={t['resolution']}" for t in pending_txn), "LIVE")
    sec("LATEST PHASE SUMMARY (compacted, derived)", latest_summary_text(slug), "DERIVED")
    sec("RECENT DECISIONS", read_tail(p / "DECISIONS.jsonl", 30), "APPEND_ONLY")
    sec("RECENT EVENTS", read_tail(p / "EVENTS.jsonl", 35), "APPEND_ONLY")

    relevant = []
    if mode != "hot" and (query or "").strip():
        relevant = indexmod.fts_search(slug, query, cfg["search_results"] if mode == "smart" else cfg["search_results"] * 2)

    header = [
        "# GENERATED AI CONTEXT PACK — NOT CANONICAL",
        f"AI_MEMORY_OS_VERSION: {core.VERSION}",
        f"PROJECT: {man.get('name', slug)}", f"SLUG: {slug}", f"GENERATED: {iso()}",
        f"QUERY: {query or '(none)'}", f"MODE: {mode}",
        f"REQUESTED_TOKEN_BUDGET: {requested_budget}",
        f"APPROX_TOKEN_BUDGET: {token_budget}",
        f"MEMORY_VERSION: {man.get('memory_version', 0)}",
        f"RECONCILIATION: {reconciliation['status']}" + (f" FLAGS={','.join(reconciliation['flags'])}" if reconciliation['flags'] else ""),
        "",
        "AUTHORITY ORDER:",
        "1. Explicit current-session user instruction",
        "2. Freshly verified physical repo/runtime state (overrides stale memory)",
        "3. CURRENT.md / NEXT.md",
        "4. Latest immutable checkpoint",
        "5. Provenance facts, decisions/events, retrieved history",
        "",
        "RULE: Do not scan the full memory archive unless this task requires it.",
        "RULE: Generated context packs are disposable; update canonical files, never this pack.",
        "RULE: Bind every aimem-step and aimem finish to the exact SESSION_ID.",
    ]
    if reconmod.needs_attention(reconciliation):
        header += ["", f"⚠ RECONCILIATION_{reconciliation['status']}: " + " ".join(reconciliation.get("detail", [])[:2])]
    if pending_txn:
        header += [f"⚠ UNRESOLVED_TRANSACTIONS={len(pending_txn)}"]

    out = "\n".join(header) + "\n"
    total_chars = int(token_budget * 3.2)
    reserve = int(total_chars * (0.38 if relevant else 0.0))
    hot_limit = total_chars - reserve
    for title, body, authority in sections:
        block = f"\n---\n## {title}\nAUTHORITY: {authority}\n{body}\n"
        if len(out) + len(block) <= hot_limit:
            out += block
        else:
            room = hot_limit - len(out)
            if room > 300:
                out += f"\n---\n## {title}\nAUTHORITY: {authority}\n" + body[:max(0, room - 120)] + "\n[TRUNCATED]\n"
            break
    if relevant and len(out) < total_chars:
        out += "\n---\n# QUERY-RELEVANT RETRIEVAL (ranked locally)\n"
        seen = set()
        for r in relevant:
            key = (r["path"], r["chunk_no"])
            if key in seen or r["kind"] in {"hot_current", "hot_next"}:
                continue
            seen.add(key)
            block = f"\n## {r['path']}#{r['chunk_no']}\nKIND: {r['kind']} SCORE: {r['score']:.2f}\n{r['body']}\n"
            if len(out) + len(block) > total_chars:
                room = total_chars - len(out)
                if room > 350:
                    out += block[:room - 30] + "\n[TRUNCATED]\n"
                break
            out += block
    out = out[:total_chars]
    out += f"\n---\nAPPROX_CONTEXT_TOKENS: {approx_tokens(out)}\n"
    return out
