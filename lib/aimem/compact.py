"""Layered, deterministic, rebuildable memory compaction (no LLM, no API).

raw steps (cold/step-journal/<day>.jsonl)      -> never deleted
  -> daily summaries   cold/summaries/daily/<day>.md + .json
  -> phase summaries   cold/summaries/phase/<checkpoint-id>.md + .json   (steps between checkpoints)
  -> checkpoint ledger cold/summaries/CHECKPOINTS.md

Every summary embeds the sha256 of its sources; regeneration is skipped when sources
are unchanged, so an interrupted run simply resumes. Atomic writes only.
"""
from __future__ import annotations

import json
from collections import Counter, OrderedDict
from pathlib import Path

from . import core
from .core import atomic_write, iso, project_dir, read_jsonl, sha256_file, try_load_json


def _steps_dir(slug):
    return project_dir(slug) / "cold" / "step-journal"


def _sum_dir(slug):
    return project_dir(slug) / "cold" / "summaries"


def _aggregate(steps):
    kinds = Counter(s.get("step_kind") or s.get("kind") for s in steps)
    sessions = OrderedDict()
    files = Counter()
    results = Counter()
    errors, tests, decisions, writes = [], [], [], []
    heads = OrderedDict()
    for s in steps:
        sid = s.get("session_id") or "-"
        sessions.setdefault(sid, {"agent": s.get("agent"), "steps": 0, "first": s.get("time"), "last": s.get("time")})
        sessions[sid]["steps"] += 1
        sessions[sid]["last"] = s.get("time")
        for f in s.get("files") or []:
            files[f] += 1
        if s.get("result"):
            results[str(s["result"])[:40]] += 1
        k = s.get("step_kind")
        summ = (s.get("summary") or "")[:220]
        if k == "error":
            errors.append(f"{s.get('time')} [{sid[-6:]}] {summ}")
        elif k == "test":
            tests.append(f"{s.get('time')} [{sid[-6:]}] {summ} => {s.get('result')}")
        elif k == "decision":
            decisions.append(f"{s.get('time')} [{sid[-6:]}] {summ}")
        elif k == "write":
            writes.append(f"{s.get('time')} [{sid[-6:]}] {summ}")
        rs = s.get("repo_state") or {}
        if rs.get("head"):
            heads[rs["head"]] = rs.get("branch")
    return {
        "step_count": len(steps), "kinds": dict(kinds), "sessions": sessions, "files_touched": files.most_common(40),
        "results": dict(results), "errors": errors[-25:], "tests": tests[-25:], "decisions": decisions[-25:],
        "writes": writes[-40:], "repo_heads": list(heads.items())[-10:],
        "first_time": steps[0].get("time") if steps else None, "last_time": steps[-1].get("time") if steps else None,
    }


def _render(title, agg, sources, extra=None):
    L = [f"# {title}", "", "DERIVED_SUMMARY: rules-based aggregation of raw step journal; raw evidence retained in cold storage.",
         f"GENERATED: {iso()}", f"STEPS: {agg['step_count']}  SPAN: {agg['first_time']} .. {agg['last_time']}",
         "SOURCES: " + ", ".join(f"{Path(s).name}@{h[:12]}" for s, h in sources.items()), ""]
    if extra:
        L += extra + [""]
    L.append("## Step kinds")
    L += [f"- {k}: {v}" for k, v in sorted(agg["kinds"].items(), key=lambda x: str(x[0]))]
    L += ["", "## Sessions"]
    L += [f"- {sid} agent={v['agent']} steps={v['steps']} {v['first']} .. {v['last']}" for sid, v in agg["sessions"].items()]
    if agg["results"]:
        L += ["", "## Results"] + [f"- {k}: {v}" for k, v in sorted(agg["results"].items())]
    if agg["repo_heads"]:
        L += ["", "## Repo heads observed"] + [f"- {h[:12]} ({b})" for h, b in agg["repo_heads"]]
    if agg["files_touched"]:
        L += ["", "## Files touched (top)"] + [f"- {f} ×{n}" for f, n in agg["files_touched"][:25]]
    for name, key in (("Decisions", "decisions"), ("Tests", "tests"), ("Errors", "errors"), ("Writes", "writes")):
        if agg[key]:
            L += ["", f"## {name}"] + [f"- {x}" for x in agg[key]]
    return "\n".join(L) + "\n"


def compact_project(slug, dry_run=False, rebuild=False):
    p = project_dir(slug)
    sd = _steps_dir(slug)
    out = {"project": slug, "daily_written": 0, "daily_skipped": 0, "phase_written": 0, "phase_skipped": 0, "ledger": False}
    days = sorted(sd.glob("*.jsonl")) if sd.exists() else []
    # ---- daily
    for f in days:
        day = f.stem
        target_md = _sum_dir(slug) / "daily" / f"{day}.md"
        target_js = _sum_dir(slug) / "daily" / f"{day}.json"
        src_sha = sha256_file(f)
        old = try_load_json(target_js, {}) or {}
        if not rebuild and old.get("sources", {}).get(str(f)) == src_sha:
            out["daily_skipped"] += 1
            continue
        steps = read_jsonl(f)
        agg = _aggregate(steps)
        sources = {str(f): src_sha}
        if dry_run:
            out["daily_written"] += 1
            continue
        atomic_write(target_md, _render(f"{slug} — daily summary {day}", agg, sources))
        atomic_write(target_js, core.dumps({"layer": "daily", "day": day, "sources": sources, "aggregate": agg, "generated": iso()}),
                     validate=core.validate_json_file)
        out["daily_written"] += 1
    # ---- phase (between checkpoints)
    cps = sorted((p / "checkpoints").glob("*/meta.json")) if (p / "checkpoints").exists() else []
    metas = [m for m in (try_load_json(c, None) for c in cps) if m]
    metas.sort(key=lambda m: m.get("time") or "")
    all_steps = []
    for f in days:
        all_steps += read_jsonl(f)
    all_steps.sort(key=lambda s: s.get("time") or "")
    prev_time = ""
    ledger = ["# CHECKPOINT LEDGER (derived)", "", f"GENERATED: {iso()}", ""]
    for m in metas:
        cp_id = m.get("id")
        t = m.get("time") or ""
        window = [s for s in all_steps if prev_time < (s.get("time") or "") <= t]
        src = {str(f): sha256_file(f) for f in days if any(prev_time < (s.get("time") or "") <= t for s in read_jsonl(f))}
        target_md = _sum_dir(slug) / "phase" / f"{cp_id}.md"
        target_js = _sum_dir(slug) / "phase" / f"{cp_id}.json"
        old = try_load_json(target_js, {}) or {}
        fingerprint = core.sha256_text(json.dumps({"src": src, "cp": cp_id, "prev": prev_time}, sort_keys=True))
        ledger.append(f"- {cp_id} result={m.get('result')} session={m.get('session_id')} steps={len(window)} advances_current={m.get('advances_current_checkpoint')}")
        if not rebuild and old.get("fingerprint") == fingerprint:
            out["phase_skipped"] += 1
        else:
            agg = _aggregate(window)
            extra = [f"CHECKPOINT: {cp_id}", f"RESULT: {m.get('result')}", f"LABEL: {m.get('label')}", f"SESSION: {m.get('session_id')}",
                     f"SUMMARY: {(m.get('summary') or '')[:600]}", f"REPO_HEAD: {(m.get('repo_state') or {}).get('head')}",
                     f"PREVIOUS_CHECKPOINT_TIME: {prev_time or '(start)'}", f"CURRENT_SHA256: {m.get('current_sha256')}"]
            if not dry_run:
                atomic_write(target_md, _render(f"{slug} — phase summary {cp_id}", agg, src, extra))
                atomic_write(target_js, core.dumps({"layer": "phase", "checkpoint": cp_id, "fingerprint": fingerprint, "sources": src,
                                                    "aggregate": agg, "checkpoint_meta_sha256": sha256_file(p / "checkpoints" / cp_id / "meta.json")
                                                    if (p / "checkpoints" / cp_id / "meta.json").exists() else None, "generated": iso()}),
                             validate=core.validate_json_file)
            out["phase_written"] += 1
        prev_time = t
    if metas and not dry_run:
        atomic_write(_sum_dir(slug) / "CHECKPOINTS.md", "\n".join(ledger) + "\n")
        out["ledger"] = True
    return out
