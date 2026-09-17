"""ChatGPT bridge — local, no API.

chat-export: self-contained context file for a ChatGPT conversation (AI_MEMORY_CHATGPT_CONTEXT_V1).
chat-import: safe ingestion of AI_MEMORY_AGENT_PACKET_V1 packets produced by ChatGPT.
  - packet preserved verbatim (object store + knowledge/chat-imports copy), hashed
  - format validated, claims compared with CURRENT.md and fresh physical state
  - never overwrites CURRENT.md; produces a reconciliation report; conflicts fail closed (exit 3)
"""
from __future__ import annotations

import re
from pathlib import Path

from . import core, objects, provenance
from .core import iso, project_dir, stamp

PACKET_VERSION = "AI_MEMORY_AGENT_PACKET_V1"
EXPORT_VERSION = "AI_MEMORY_CHATGPT_CONTEXT_V1"
HEX40 = re.compile(r"\b[0-9a-f]{40}\b")
KEY_RX = re.compile(r"^([A-Z][A-Z0-9_ /-]{1,60}):\s*(.*)$")
SECTION_RX = re.compile(r"^(?:#+\s*)?([A-Z][A-Z0-9_]{3,})\s*$")


# ---------------------------------------------------------------- export

def export(slug, query="", tokens=6000, output=None):
    from . import context as ctxmod
    core.ensure_root()
    man = core.project_manifest(slug)
    p = project_dir(slug)
    live = core.repo_state_for(slug)
    ctx = ctxmod.build_context(slug, query, tokens, "smart", live_repo=live)
    nxt = (p / "NEXT.md").read_text(errors="ignore") if (p / "NEXT.md").exists() else ""
    head = [
        f"================ {EXPORT_VERSION} BEGIN ================",
        f"EXPORT_VERSION: {EXPORT_VERSION}", f"AI_MEMORY_OS_VERSION: {core.VERSION}",
        f"PROJECT_NAME: {man.get('name', slug)}", f"PROJECT_SLUG: {slug}", f"GENERATED: {iso()}",
        f"MEMORY_VERSION: {man.get('memory_version', 0)}", f"CURRENT_CHECKPOINT: {man.get('current_checkpoint')}",
        f"PHYSICAL_HEAD_AT_EXPORT: {live.get('head')}", f"PHYSICAL_BRANCH_AT_EXPORT: {live.get('branch')}",
        f"PHYSICAL_DIRTY_AT_EXPORT: {live.get('dirty')}", f"TASK_QUERY: {query or '(none)'}", "",
        "READER_CONTRACT (for ChatGPT):",
        "- You are reading an exported snapshot of local AI Memory. You do NOT have access to this Mac's filesystem, repositories, databases or runtime.",
        "- Do not invent, assume or extrapolate physical state (files, hashes, test results, builds). Everything not stated here is UNKNOWN.",
        "- Treat 'CURRENT AUTHORITATIVE STATE' and 'NEXT ACTIONS' as the compact truth at export time; it may already be stale.",
        "- If you produce a handoff back, emit an AI_MEMORY_AGENT_PACKET_V1 block and mark every physical claim you did not verify as REPORTED/UNCONFIRMED.",
        "- Never include secrets, credentials or tokens in a packet.", "",
        "EXACT_NEXT_ACTION:", nxt.strip() or "(NEXT.md empty)", "",
        "---- BOUNDED CONTEXT PACK (generated locally, ranked retrieval) ----",
    ]
    text = "\n".join(head) + "\n" + ctx + f"\n================ {EXPORT_VERSION} END =================\n"
    target = Path(output).expanduser() if output else p / ".generated" / f"CHATGPT-EXPORT-{stamp()}.md"
    core.atomic_write(target, text)
    core.append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "chat_export", "path": str(target), "approx_tokens": core.approx_tokens(text),
                                            "sha256": core.sha256_text(text)})
    return target, core.approx_tokens(text)


# ---------------------------------------------------------------- packet parsing

def parse_packet(text):
    """Tolerant parser for AI_MEMORY_AGENT_PACKET_V1 (keys on same or next line; ALLCAPS sections)."""
    lines = text.splitlines()
    pk = {"fields": {}, "sections": {}, "heads": [], "branches": [], "errors": [], "warnings": []}
    if "AI_MEMORY_AGENT_PACKET BEGIN" not in text:
        pk["warnings"].append("missing BEGIN marker")
    if "AI_MEMORY_AGENT_PACKET END" not in text:
        pk["warnings"].append("missing END marker")
    section = "PREAMBLE"
    pending_key = None
    for raw in lines:
        line = raw.rstrip()
        s = line.strip()
        if not s:
            continue
        if "AI_MEMORY_AGENT_PACKET" in s and ("BEGIN" in s or "END" in s):
            continue
        m = KEY_RX.match(s)
        if m:
            key, val = m.group(1).strip().replace(" ", "_"), m.group(2).strip()
            if val:
                pk["fields"].setdefault(key, val)
                pending_key = None
            else:
                pending_key = key
            pk["sections"].setdefault(section, []).append(s)
            continue
        if pending_key:
            pk["fields"].setdefault(pending_key, s)
            pending_key = None
            pk["sections"].setdefault(section, []).append(s)
            continue
        sm = SECTION_RX.match(s)
        if sm and len(s) < 70:
            section = sm.group(1)
            pk["sections"].setdefault(section, [])
            continue
        pk["sections"].setdefault(section, []).append(s)
    for h in HEX40.findall(text):
        if h not in pk["heads"]:
            pk["heads"].append(h)
    for m in re.finditer(r"(?i)branch:?\s*`?([A-Za-z0-9._/-]{3,})`?", text):
        b = m.group(1)
        if b not in pk["branches"]:
            pk["branches"].append(b)
    f = pk["fields"]
    if f.get("PACKET_VERSION") != PACKET_VERSION:
        pk["errors"].append(f"PACKET_VERSION is {f.get('PACKET_VERSION')!r}, expected {PACKET_VERSION}")
    if not f.get("PROJECT_SLUG"):
        pk["errors"].append("PROJECT_SLUG missing")
    return pk


# ---------------------------------------------------------------- import

def import_packet(slug, packet_path, dry_run=False, note="", session=None):
    core.ensure_root()
    p = project_dir(slug)
    src = Path(packet_path).expanduser()
    if not src.exists():
        core.die(f"Packet not found: {src}")
    text = src.read_text(errors="ignore")
    pk = parse_packet(text)
    sha = core.sha256_text(text)
    live = core.repo_state_for(slug)
    cur_text = (p / "CURRENT.md").read_text(errors="ignore") if (p / "CURRENT.md").exists() else ""
    cur_heads = list(dict.fromkeys(HEX40.findall(cur_text)))
    conflicts, warnings, matches = [], list(pk["warnings"]), []
    secrets = core.secret_hits_text(text)
    if secrets:
        conflicts.append("SECRET_PATTERN_IN_PACKET: " + ", ".join(secrets))
    claimed_slug = pk["fields"].get("PROJECT_SLUG")
    if claimed_slug and claimed_slug != slug:
        conflicts.append(f"PACKET_SLUG_MISMATCH: packet says {claimed_slug}, importing into {slug}")
    live_head = live.get("head")
    if pk["heads"]:
        if live_head and live_head in pk["heads"]:
            matches.append(f"packet mentions live HEAD {live_head[:12]}")
        elif live_head:
            conflicts.append(f"PHYSICAL_HEAD_NOT_IN_PACKET: live HEAD {live_head[:12]} not among packet heads {[h[:12] for h in pk['heads'][:4]]} (physical state may be newer than packet)")
        if cur_heads and any(h in cur_heads for h in pk["heads"]):
            matches.append("packet shares HEAD(s) with CURRENT.md")
        elif cur_heads:
            warnings.append("packet heads differ from CURRENT.md heads")
    else:
        warnings.append("packet contains no 40-hex commit hash")
    if live.get("dirty"):
        warnings.append("live repo is dirty; runtime verification required before applying any packet claim")
    if pk["errors"]:
        conflicts += ["PACKET_FORMAT: " + e for e in pk["errors"]]
    status = "CONFLICTS_REQUIRE_RESOLUTION" if conflicts else ("IMPORTED_WITH_WARNINGS" if warnings else "IMPORTED_CLEAN")
    report = [
        "# CHAT IMPORT RECONCILIATION — GENERATED, NOT CANONICAL",
        f"PROJECT: {slug}", f"PACKET: {src}", f"PACKET_SHA256: {sha}", f"GENERATED: {iso()}", f"DRY_RUN: {dry_run}",
        f"STATUS: {status}", f"PACKET_VERSION: {pk['fields'].get('PACKET_VERSION')}", f"PACKET_SOURCE: {pk['fields'].get('SOURCE')}",
        f"PACKET_HEADS: {', '.join(h[:12] for h in pk['heads'][:6]) or '-'}", f"PACKET_BRANCHES: {', '.join(pk['branches'][:4]) or '-'}",
        f"LIVE_HEAD: {live_head} ({live.get('branch')}) dirty={live.get('dirty')}", f"CURRENT_MD_HEADS: {', '.join(h[:12] for h in cur_heads[:4]) or '-'}",
        f"SECTIONS: {', '.join(list(pk['sections'])[:20])}", "",
    ]
    if conflicts:
        report += ["## CONFLICTS (fail closed — resolve manually; CURRENT.md untouched)"] + [f"- {c}" for c in conflicts] + [""]
    if warnings:
        report += ["## WARNINGS"] + [f"- {w}" for w in warnings] + [""]
    if matches:
        report += ["## MATCHES"] + [f"- {m}" for m in matches] + [""]
    report += ["## POLICY", "- The packet is evidence (REPORTED), never authority.", "- Newer verified physical state is never overwritten by a packet.",
               "- Update CURRENT.md manually only after verifying claims locally."]
    report_text = "\n".join(report) + "\n"
    result = {"status": status, "sha256": sha, "conflicts": conflicts, "warnings": warnings, "matches": matches, "dry_run": dry_run}
    if dry_run:
        rp = p / ".generated" / f"CHAT-IMPORT-DRYRUN-{stamp()}.md"
        core.atomic_write(rp, report_text)
        result["report"] = str(rp)
        return result
    # persist evidence verbatim
    osha, opath, dedup = objects.store_text(text)
    kdir = p / "knowledge" / "chat-imports"
    kdir.mkdir(parents=True, exist_ok=True)
    kcopy = kdir / f"{stamp()}__{src.name}"
    core.atomic_write(kcopy, text)
    rp = p / ".generated" / f"CHAT-IMPORT-{stamp()}.md"
    core.atomic_write(rp, report_text)
    rcopy = kdir / f"{kcopy.stem}.RECONCILIATION.md"
    core.atomic_write(rcopy, report_text)
    core.append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "chat_import", "source_path": str(src), "stored_path": str(kcopy),
                                            "object": osha, "sha256": sha, "status": status, "conflicts": conflicts, "warnings": warnings,
                                            "note": core.redact(note, 500), "session_id": session, "deduplicated": dedup})
    core.append_jsonl(p / "ARTIFACTS.jsonl", {"time": iso(), "kind": "chat_packet", "name": src.name, "bytes": len(text.encode()),
                                               "sha256": sha, "object": osha, "stored_path": str(kcopy), "note": "AI_MEMORY_AGENT_PACKET_V1 import"})
    for h in pk["heads"][:3]:
        provenance.record_fact(slug, "chatgpt.claimed_head", h, "CHATGPT_HANDOFF", source_ref=str(kcopy), status="REPORTED",
                               session=session, evidence_sha256=sha, note="claimed by packet; not verified")
    provenance.record_fact(slug, "chatgpt.last_import", str(kcopy.name), "CHATGPT_HANDOFF", source_ref=str(kcopy), status="VERIFIED",
                           session=session, evidence_sha256=sha, note=status)
    from . import index as indexmod
    indexmod.reindex(slug, quiet=True)
    result.update({"stored_path": str(kcopy), "object": str(opath), "report": str(rp), "deduplicated": dedup})
    return result
