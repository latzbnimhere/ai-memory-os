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


# Journal kinds that are pure machine state: their evidence stays in cold storage / provenance.
NOISE_EVENT_KINDS = {"repo_capture", "chat_export"}


def _short(v, n):
    v = " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


def render_record(r):
    """One compact, human-readable line per journal record (instead of raw multi-KB JSON)."""
    t = (r.get("time") or "")[:19]
    kind = r.get("kind") or "?"
    if kind == "operational_step":
        kind = f"step/{r.get('step_kind')}"
    bits = [f"- {t} [{kind}]"]
    if kind == "checkpoint":
        bits.append(f"{r.get('id')} result={r.get('result')} v{r.get('memory_version')}")
    else:
        text = (r.get("summary") or r.get("text") or r.get("task") or r.get("label") or r.get("note")
                or r.get("reason") or "")
        if text:
            bits.append(_short(text, 220))
        for k in ("result", "checkpoint", "status", "reconciliation"):
            if r.get(k) not in (None, ""):
                bits.append(f"{k}={_short(r[k], 60)}")
        if r.get("memory_version") is not None:
            bits.append(f"v{r['memory_version']}")
    if r.get("repo_head"):
        bits.append(f"head={str(r['repo_head'])[:12]}")
    if r.get("session_id"):
        bits.append(f"session=…{str(r['session_id'])[-6:]}")
    return " ".join(bits)


def journal_text(path, n, skip_kinds=()):
    """Last n meaningful records, compactly rendered, consecutive duplicates collapsed."""
    recs = [r for r in core.tail_jsonl(path, n * 3) if r.get("kind") not in skip_kinds][-n:]
    lines = []
    for r in recs:
        line = render_record(r)
        body = line.split("]", 1)[-1]
        if lines and lines[-1][0] == body:
            lines[-1][2] += 1
            continue
        lines.append([body, line, 1])
    return "\n".join(l if c == 1 else f"{l} (×{c})" for _b, l, c in lines)


TRUNC_MARK = "\n[TRUNCATED]\n"
TAIL_MARK = "[EARLIER ENTRIES TRUNCATED]\n"


def allocate(sections, budget):
    """Priority-aware budget split. sections: dicts with title/body/authority/priority/cap.

    Pass 1 gives each section, in priority order, up to its `cap` share of the budget.
    Pass 2 hands leftover space to still-truncated sections, again by priority.
    NEXT (priority 1) and CURRENT (priority 2) therefore always appear even when CURRENT
    alone exceeds the budget. Invariant: total rendered length <= budget.
    Returns rendered blocks in the sections' display order.
    """
    for sec in sections:
        sec["head"] = f"\n---\n## {sec['title']}\nAUTHORITY: {sec['authority']}\n"
        sec["take"] = 0

    def cost(sec, n):  # exact rendered length of sec with n body chars (see rendering below)
        if n >= len(sec["body"]):
            return len(sec["head"]) + len(sec["body"]) + 1
        return len(sec["head"]) + n + (len(TAIL_MARK) + 1 if sec.get("keep_tail") else len(TRUNC_MARK))

    def worth(sec, n):  # never emit a uselessly tiny fragment
        return n >= min(len(sec["body"]), 200)

    remaining = budget
    order = sorted(sections, key=lambda x: x["priority"])
    for sec in order:
        want = min(len(sec["body"]), max(200, int(budget * sec["cap"]) - len(sec["head"])))
        n = min(want, remaining - len(sec["head"]) - len(TAIL_MARK) - 1)
        if n > 0 and worth(sec, n):
            sec["take"] = n
            remaining -= cost(sec, n)
    for sec in order:
        if sec["take"] >= len(sec["body"]):
            continue
        base = cost(sec, sec["take"]) if sec["take"] else 0
        n = min(len(sec["body"]), remaining + base - len(sec["head"]) - len(TAIL_MARK) - 1)
        if n > sec["take"] and worth(sec, n):
            remaining -= cost(sec, n) - base
            sec["take"] = n
    out = []
    for sec in sections:
        n, body = sec["take"], sec["body"]
        if not n:
            continue
        if n >= len(body):
            out.append(sec["head"] + body + "\n")
        elif sec.get("keep_tail"):
            # chronological journals: keep the NEWEST entries, drop whole older lines
            tail = body[len(body) - n:]
            tail = tail.split("\n", 1)[1] if "\n" in tail else tail
            out.append(sec["head"] + TAIL_MARK + tail + "\n")
        else:
            out.append(sec["head"] + body[:n] + TRUNC_MARK)
    return out


def build_context(slug, query, token_budget, mode="smart", reconciliation=None, live_repo=None, pending_txn=None):
    from . import index as indexmod
    from . import reconcile as reconmod
    p = project_dir(slug)
    man = core.project_manifest(slug)
    cfg = config()
    live_repo = live_repo if live_repo is not None else core.repo_state_for(slug)
    reconciliation = reconciliation or reconmod.reconcile(slug, live_repo)

    requested_budget = max(1200, int(token_budget or cfg["default_context_tokens"]))
    attention = reconmod.needs_attention(reconciliation)
    needs_attention = bool(attention or live_repo.get("dirty") or pending_txn)

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

    def sec(title, text, authority, priority, cap, keep_tail=False):
        if text and text.strip():
            sections.append({"title": title, "body": text.strip(), "authority": authority, "priority": priority, "cap": cap,
                             "keep_tail": keep_tail})

    def read(f):
        return f.read_text(errors="ignore") if f.exists() else ""

    # Display order below is the order an agent reads; `priority` decides who keeps space under pressure.
    sec("GLOBAL OWNER RULES", read(ROOT / "global" / "OWNER_RULES.md"), "GLOBAL", 4, 0.12)
    sec("GLOBAL PREFERENCES", read(ROOT / "global" / "PREFERENCES.md"), "GLOBAL", 13, 0.08)
    compact_man = {k: man.get(k) for k in ("slug", "name", "repo", "current_checkpoint", "memory_version", "updated_at", "last_writer_session")}
    sec("PROJECT MANIFEST", json.dumps(compact_man, indent=2), "STRUCTURED", 7, 0.08)
    sec("MEMORY/PHYSICAL RECONCILIATION", reconmod.render(reconciliation), "GENERATED_FRESH", 3 if attention else 9, 0.15)
    if live_repo.get("is_git_repo"):
        sec("FRESH PHYSICAL REPO STATE", json.dumps({k: live_repo.get(k) for k in ("path", "branch", "head", "tree", "dirty", "captured_at")}, indent=2)
            + ("\nSTATUS_LINES:\n" + "\n".join(live_repo.get("status_lines", [])[:25]) if live_repo.get("dirty") else ""),
            "PHYSICAL_FRESH", 6, 0.1)
    sec("CURRENT AUTHORITATIVE STATE", read(p / "CURRENT.md"), "CURRENT", 2, 0.5)
    sec("NEXT ACTIONS", read(p / "NEXT.md"), "CURRENT", 1, 0.25)
    sec("PROVENANCE FACTS (latest per key)", provenance.compact_view_text(slug, live_repo), "STRUCTURED_PROVENANCE", 8, 0.1)
    sec("OPEN SESSIONS / LEASES", open_session_lines(slug), "LIVE", 5, 0.08)
    if pending_txn:
        sec("UNRESOLVED TRANSACTIONS", "\n".join(f"- {t['id']} state={t['state']} resolution={t['resolution']}" for t in pending_txn), "LIVE", 3, 0.08)
    sec("LATEST PHASE SUMMARY (compacted, derived)", latest_summary_text(slug), "DERIVED", 11, 0.12)
    sec("RECENT DECISIONS", journal_text(p / "DECISIONS.jsonl", 30), "APPEND_ONLY", 10, 0.15, keep_tail=True)
    sec("RECENT EVENTS", journal_text(p / "EVENTS.jsonl", 35, NOISE_EVENT_KINDS), "APPEND_ONLY", 12, 0.15, keep_tail=True)

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
        f"CURRENT_CHECKPOINT: {man.get('current_checkpoint') or '(none)'}",
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
        "RULE: Sections marked [TRUNCATED] are incomplete; read the canonical file if the omitted part matters.",
    ]
    if attention:
        header += ["", f"⚠ RECONCILIATION_{reconciliation['status']}: " + " ".join(reconciliation.get("detail", [])[:2])]
    if pending_txn:
        header += [f"⚠ UNRESOLVED_TRANSACTIONS={len(pending_txn)}"]

    out = "\n".join(header) + "\n"
    footer_reserve = 40
    total_chars = int(token_budget * 3.2) - footer_reserve
    # Retrieval may use up to 38% of the budget; whatever it does not use goes back to the hot sections.
    retrieval = ""
    if relevant:
        limit = int(total_chars * 0.38)
        retrieval = "\n---\n# QUERY-RELEVANT RETRIEVAL (ranked locally)\n"
        seen = set()
        for r in relevant:
            key = (r["path"], r["chunk_no"])
            if key in seen or r["kind"] in {"hot_current", "hot_next"}:
                continue  # CURRENT/NEXT are already in the hot section
            seen.add(key)
            block = f"\n## {r['path']}#{r['chunk_no']}\nKIND: {r['kind']} SCORE: {r['score']:.2f}\n{r['body']}\n"
            if len(retrieval) + len(block) > limit:
                room = limit - len(retrieval)
                if room > 350:
                    retrieval += block[:room - 30] + TRUNC_MARK
                break
            retrieval += block
        if not seen:
            retrieval = ""
    out += "".join(allocate(sections, max(0, total_chars - len(out) - len(retrieval))))
    out += retrieval
    out = core.redact_high_confidence(out[:total_chars])
    out += f"\n---\nAPPROX_CONTEXT_TOKENS: {approx_tokens(out)}\n"
    return out
