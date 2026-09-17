"""aimem command-line interface (V4). All V3.1.1 commands preserved; new commands added."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import core
from .core import (ROOT, append_jsonl, atomic_write, atomic_write_json, config, die, iso, project_dir, registry,
                   sha256_file, stamp)

AGENTS = ["claude", "codex", "gemini", "cursor", "chatgpt", "other"]


# ---------------------------------------------------------------- basic

def cmd_version(a):
    print(f"aimem {core.VERSION}")
    print(f"root={ROOT}")
    print(f"installed_version={core.installed_version()}")
    print("lineage=" + " -> ".join(core.ENGINE_LINEAGE))


def cmd_init(a):
    """Create a new (empty) memory root. Refuses to touch an existing populated root."""
    root = Path(a.path).expanduser() if a.path else ROOT
    if (root / "registry" / "projects.json").exists() and not a.force:
        die(f"Root already initialized: {root}")
    for d in ("registry", "projects", "global", "templates", "docs", "objects/sha256", ".locks", ".run", "logs"):
        (root / d).mkdir(parents=True, exist_ok=True)
    if not (root / "registry" / "projects.json").exists():
        atomic_write(root / "registry" / "projects.json", json.dumps({"version": 1, "projects": {}}, indent=2) + "\n")
    if not (root / "config.json").exists():
        atomic_write_json(root / "config.json", dict(core.DEFAULT_CONFIG))
    atomic_write(root / "VERSION", core.VERSION + "\n")
    if not (root / "PATCH_LEVEL").exists():
        atomic_write(root / "PATCH_LEVEL", f"AI_MEMORY_OS_PATCH={core.PATCH_LEVEL}\nAI_MEMORY_OS_VERSION={core.VERSION}\nAPPLIED_AT={iso()}\n")
    if not (root / "templates" / "CURRENT.md").exists():
        atomic_write(root / "templates" / "CURRENT.md", "# {{PROJECT_NAME}} — CURRENT AUTHORITATIVE STATE\n\nLAST_UPDATED: {{DATE}}\nSTATUS: UNINITIALIZED\nAUTHORITY_LEVEL: CURRENT\n\n## Identity\n- Project:\n- Repo / workspace:\n- Branch:\n- HEAD / source hash:\n\n## Current phase\n- Phase:\n- Result:\n\n## Authoritative facts\n- \n\n## Completed / sealed\n- \n\n## Known blockers\n- \n\n## Hard rules\n- \n\n## Exact next action\n- \n\n## Verification needed\n- \n")
        atomic_write(root / "templates" / "NEXT.md", "# {{PROJECT_NAME}} — NEXT\n\nLAST_UPDATED: {{DATE}}\n\n## Next action\n- \n\n## Stop conditions\n- \n\n## Verification after action\n- \n\n## Deferred\n- \n")
    if not (root / "global" / "OWNER_RULES.md").exists():
        atomic_write(root / "global" / "OWNER_RULES.md", "# GLOBAL OWNER / EXECUTION RULES\n\n- Read current project memory before acting.\n- Prefer read-only verification before mutations.\n- Keep secrets and credentials out of memory files.\n")
    if not (root / ".gitignore").exists():
        atomic_write(root / ".gitignore", "registry/memory.db\nregistry/memory.db-wal\nregistry/memory.db-shm\n.locks/\n.run/\nprojects/*/.generated/\nprojects/*/.txn/\n**/*.staged\n")
    print(f"INIT=PASS root={root} version={core.VERSION}")


def cmd_status(a):
    from . import sessions
    core.ensure_root()
    reg = registry()
    if not reg["projects"]:
        print("No projects.")
        return
    for slug, rec in sorted(reg["projects"].items()):
        man = core.try_load_json(project_dir(slug) / "project.json", {}) or {}
        opens = sessions.open_sessions(slug)
        leases = ",".join(sessions.lease_state(j) for f, j in opens) or "-"
        rs = core.repo_state_for(slug)
        head = (rs.get("head") or "-")[:12]
        print(f"{slug}\t{rec.get('name', slug)}\tcheckpoint={man.get('current_checkpoint') or '-'}\tv{man.get('memory_version', 0)}\topen_sessions={len(opens)}[{leases}]\thead={head}{' DIRTY' if rs.get('dirty') else ''}")


def cmd_register(a):
    from . import index
    core.ensure_root()
    slug = a.slug
    if not slug or any(c for c in slug if not (c.isalnum() or c in "-_")):
        die("slug must be alphanumeric with - or _")
    repo = (a.repo or "").strip()
    if repo and core.recursive_index_risk(repo):
        die(f"RECURSIVE_INDEX_RISK: {repo} overlaps the memory root {ROOT}; refusing to register.")
    reg = registry()
    p = project_dir(slug)
    if slug in reg["projects"] and not a.update:
        die(f"Project already registered: {slug} (use --update to change repo/name)")
    reg["projects"][slug] = {"name": a.name or slug, "path": str(p), "repo": repo}
    for sub in ("checkpoints", "knowledge", "artifacts", "cold/step-journal", "sessions", ".generated", ".txn"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    man = core.try_load_json(p / "project.json", {}) or {}
    man.update({"slug": slug, "name": a.name or man.get("name") or slug, "repo": repo, "updated_at": iso(), "engine_version": core.VERSION})
    man.setdefault("created_at", iso())
    man.setdefault("current_checkpoint", None)
    man.setdefault("memory_version", 0)
    atomic_write_json(p / "project.json", man)
    for f in ("DECISIONS.jsonl", "EVENTS.jsonl", "ARTIFACTS.jsonl", "PROVENANCE.jsonl"):
        (p / f).touch()
    tmpl = ROOT / "templates"
    if not (p / "CURRENT.md").exists():
        t = (tmpl / "CURRENT.md").read_text() if (tmpl / "CURRENT.md").exists() else "# {{PROJECT_NAME}} — CURRENT AUTHORITATIVE STATE\n\nLAST_UPDATED: {{DATE}}\nSTATUS: UNINITIALIZED\n\n## Exact next action\n- \n"
        atomic_write(p / "CURRENT.md", t.replace("{{PROJECT_NAME}}", man["name"]).replace("{{DATE}}", iso()))
    if not (p / "NEXT.md").exists():
        t = (tmpl / "NEXT.md").read_text() if (tmpl / "NEXT.md").exists() else "# {{PROJECT_NAME}} — NEXT\n\nLAST_UPDATED: {{DATE}}\n\n## Next action\n- \n"
        atomic_write(p / "NEXT.md", t.replace("{{PROJECT_NAME}}", man["name"]).replace("{{DATE}}", iso()))
    core.save_registry(reg)
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "project_registered", "slug": slug, "repo": repo})
    core.rebuild_master_index()
    index.reindex(slug, quiet=True)
    core.git_memory_commit(f"{slug}: registered")
    print(f"REGISTERED={slug} repo={repo or '(none)'}")


def cmd_doctor(a):
    from . import doctor
    errors, warnings, info = doctor.run(deep=a.deep, check_repo=not a.no_repo, repair=a.repair, slug_filter=a.slug)
    rc = doctor.print_report(errors, warnings, info)
    raise SystemExit(rc)


def cmd_reindex(a):
    from . import index
    index.reindex(a.slug)


def cmd_search(a):
    from . import index
    core.ensure_root()
    slugs = core.all_slugs() if a.all else ([a.slug] if a.slug else die("Specify a project slug or --all"))
    found = 0
    for slug in slugs:
        for r in index.fts_search(slug, a.query, a.limit):
            found += 1
            print(f"[{slug}] {r['path']}#{r['chunk_no']} score={r['score']:.3f} kind={r['kind']}")
            print(r["snippet"].replace("\n", " "))
            print()
    if not found:
        print("NO_MATCHES")


def cmd_context(a):
    from . import context
    core.ensure_root()
    budget = a.tokens or config()["default_context_tokens"]
    text = context.build_context(a.slug, a.query, budget, a.mode)
    target = context.write_context(a.slug, text)
    if a.output:
        out = Path(a.output).expanduser()
        atomic_write(out, text)
        print(out)
    elif a.stdout:
        print(text)
    else:
        print(target)
        print(f"approx_tokens={core.approx_tokens(text)} budget={budget}")


# ---------------------------------------------------------------- sessions

def cmd_begin(a):
    from . import sessions
    state, out = sessions.begin(a.slug, a.agent, a.task, a.tokens, a.mode)
    print("\n".join(out))


def cmd_heartbeat(a):
    from . import sessions
    core.ensure_root()
    sid, binding = sessions.resolve_session(a.slug, a.session)
    if not sid:
        die(f"No open session for {a.slug}")
    j = sessions.touch_heartbeat(a.slug, sid, a.note)
    print(f"HEARTBEAT=OK session={sid} binding={binding} at={j['lease']['last_heartbeat']}")


def cmd_finish(a):
    from . import sessions
    cp, version = sessions.finish(a.slug, a.session, a.result, a.label, a.summary or "", a.allow_unchanged, a.no_advance_current,
                                  a.expect_version, a.acknowledge_newer)
    print("FINISH=PASS")
    print(f"CHECKPOINT={cp}")
    print(f"MEMORY_VERSION={version}")


def cmd_checkpoint(a):
    from . import sessions
    core.ensure_root()
    cp, version = sessions.create_checkpoint(a.slug, a.label, a.result, a.summary or "", a.session, not a.no_advance_current, a.expect_version)
    from . import index
    index.reindex(a.slug, quiet=True)
    print(cp)
    print(f"MEMORY_VERSION={version}")


def cmd_sessions(a):
    from . import sessions
    core.ensure_root()
    slugs = [a.slug] if a.slug else core.all_slugs()
    for slug in slugs:
        for f, j in sessions.all_sessions(slug):
            if a.open and j.get("status") != "OPEN":
                continue
            s = sessions.session_summary(slug, f, j)
            print(f"{slug}\t{s['id']}\t{s['status']}/{s['lease']}\tagent={s['agent']}\tstarted={s['started_at']}\thb={s['last_heartbeat']}\tresult={s['result'] or '-'}\ttask={s['task'][:70]}")


def cmd_session(a):
    from . import sessions
    core.ensure_root()
    if a.action == "close":
        if not a.session:
            die("--session is required (explicit binding)")
        st = sessions.close_session(a.slug, a.session, a.result, a.note or "")
        print(f"SESSION_CLOSED={st['id']} result={st['result']}")
    elif a.action == "show":
        sid = a.session or sessions.resolve_session(a.slug, None)[0]
        if not sid:
            die("no session")
        j = sessions.load_session(a.slug, sid)
        j["lease_state"] = sessions.lease_state(j)
        j["last_step"] = sessions.last_step(a.slug, sid)
        print(json.dumps(j, indent=2, ensure_ascii=False))


def cmd_write_current(a):
    """Transactional, compare-and-swap update of CURRENT.md / NEXT.md from files."""
    from . import txn
    core.ensure_root()
    p = project_dir(a.slug)
    files = {}
    if a.current:
        files[p / "CURRENT.md"] = Path(a.current).expanduser().read_text()
    if a.next:
        files[p / "NEXT.md"] = Path(a.next).expanduser().read_text()
    if not files:
        die("Provide --current <file> and/or --next <file>")
    for path, text in files.items():
        hits = core.secret_hits_text(text)
        if hits and not a.allow_secret_pattern:
            die(f"SECRET_PATTERN_REJECTED in {path.name}: {', '.join(hits)}")
    version, txid = txn.canonical_update(a.slug, files, session=a.session, expect_version=a.expect_version, kind="write_current")
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "canonical_write", "session_id": a.session, "files": [x.name for x in files],
                                       "memory_version": version, "txn": txid})
    print(f"WRITE=PASS memory_version={version} txn={txid}")


def cmd_txn(a):
    from . import txn
    core.ensure_root()
    slugs = [a.slug] if a.slug else core.all_slugs()
    total = 0
    for slug in slugs:
        if a.repair:
            for rid, action in txn.repair(slug):
                print(f"{slug}\t{rid}\t{action}")
                total += 1
        else:
            for t in txn.inspect(slug):
                total += 1
                print(f"{slug}\t{t['id']}\tstate={t['state']}\tresolution={t['resolution']}\ttargets={[Path(x).name for x in t['targets']]}")
    if not total:
        print("NO_PENDING_TRANSACTIONS")
    elif not a.repair:
        raise SystemExit(core.EXIT_RECOVERY_REQUIRED)


# ---------------------------------------------------------------- recovery / reconcile

def cmd_recover(a):
    from . import recover, sessions
    core.ensure_root()
    if a.session:
        slugs = [a.slug] if a.slug else core.all_slugs()
        for slug in slugs:
            if sessions.session_file(slug, a.session).exists():
                asmt, text, out = recover.packet(slug, a.session)
                print(text)
                print(f"RECOVERY_CONTEXT={out}")
                raise SystemExit(core.EXIT_RECOVERY_REQUIRED if asmt["recovery_required"] else 0)
        die(f"session not found: {a.session}")
    slugs = [a.slug] if a.slug else core.all_slugs()
    required = 0
    for slug in slugs:
        for asmt in recover.scan(slug):
            flag = "RECOVERY_REQUIRED" if asmt["recovery_required"] else "OPEN_OK"
            print(f"{slug}\t{asmt['id']}\tlease={asmt['lease']}\t{flag}\t{'; '.join(asmt['reasons']) or '-'}")
            if asmt["recovery_required"]:
                required += 1
                _, _, out = recover.packet(slug, asmt["id"])
                print(f"  RECOVERY_CONTEXT={out}")
    print(f"RECOVERY_REQUIRED_COUNT={required}")
    raise SystemExit(core.EXIT_RECOVERY_REQUIRED if required else 0)


def cmd_reconcile(a):
    from . import reconcile
    core.ensure_root()
    r = reconcile.reconcile(a.slug)
    print(reconcile.render(r))
    raise SystemExit(0 if r["status"] == "MEMORY_MATCH" and not r["flags"] else core.EXIT_WARN)


# ---------------------------------------------------------------- provenance / facts

def cmd_fact(a):
    from . import provenance
    core.ensure_root()
    if a.action == "set":
        if not (a.key and a.value is not None and a.source_type):
            die("fact set requires --key --value --source-type")
        r = provenance.record_fact(a.slug, a.key, a.value, a.source_type, a.source_ref or "", a.status, a.session, a.evidence, a.note or "")
        print(f"FACT_RECORDED={r['fact_id']} key={r['key']} status={r['status']}")
    else:
        live = core.repo_state_for(a.slug)
        view = provenance.latest_view(a.slug, live)
        for k in sorted(view):
            if a.key and a.key != k:
                continue
            f = view[k]
            print(f"{k}\t{f.get('value')}\t{f.get('status')}\t{f.get('source_type')}\t{f.get('verified_at') or f.get('time')}\t{f.get('session') or '-'}")
        if not view:
            print("NO_FACTS")


# ---------------------------------------------------------------- compaction / objects / artifacts

def cmd_compact(a):
    from . import compact
    core.ensure_root()
    slugs = core.all_slugs() if a.all else ([a.slug] if a.slug else die("Specify slug or --all"))
    for slug in slugs:
        r = compact.compact_project(slug, dry_run=a.dry_run, rebuild=a.rebuild)
        print(f"COMPACT={'DRY_RUN' if a.dry_run else 'PASS'} project={slug} daily_written={r['daily_written']} daily_skipped={r['daily_skipped']} phase_written={r['phase_written']} phase_skipped={r['phase_skipped']}")
    if not a.dry_run:
        from . import index
        for slug in slugs:
            index.reindex(slug, quiet=True)


def cmd_objects(a):
    from . import objects
    core.ensure_root()
    if a.action == "verify":
        ok, bad, misplaced = objects.verify_all()
        broken = objects.broken_references()
        print(f"OBJECTS_VERIFY={'PASS' if not bad and not misplaced and not broken else 'FAIL'} ok={ok} mismatched={len(bad)} misplaced={len(misplaced)} broken_refs={len(broken)}")
        for path, e, act in bad:
            print(f"MISMATCH {path} expected={e[:12]} actual={act[:12]}")
        for sha, where in broken:
            print(f"BROKEN_REF {sha} from {where}")
        raise SystemExit(0 if not bad and not misplaced and not broken else 1)
    s = objects.stats()
    print(f"OBJECTS={s['objects']} bytes={s['bytes']} ({core.human_bytes(s['bytes'])}) root={core.OBJECTS}")


def cmd_artifact(a):
    from . import objects
    core.ensure_root()
    p = project_dir(a.slug)
    src = Path(a.path).expanduser().resolve()
    if not src.exists() or not src.is_file():
        die(f"File not found: {src}")
    rec = {"time": iso(), "kind": a.kind, "source_path": str(src), "name": src.name, "bytes": src.stat().st_size,
           "sha256": sha256_file(src), "note": core.redact(a.note or "", 1000), "session_id": a.session}
    if a.copy or a.store:
        sha, opath, dedup = objects.store_file(src)
        rec["object"] = sha
        rec["stored_path"] = str(opath)
        rec["deduplicated"] = dedup
    append_jsonl(p / "ARTIFACTS.jsonl", rec)
    print(json.dumps(rec, indent=2))


def cmd_capture(a):
    from . import provenance
    core.ensure_root()
    p = project_dir(a.slug)
    state = core.repo_state_for(a.slug)
    atomic_write_json(p / "REPO_STATE.json", state)
    append_jsonl(p / "EVENTS.jsonl", {"time": iso(), "kind": "repo_capture", "repo_state": state})
    provenance.record_physical_git(a.slug, state, session=a.session, source_ref="capture")
    print(json.dumps(state, indent=2))


def cmd_note(a):
    core.ensure_root()
    p = project_dir(a.slug)
    if not p.exists():
        die(f"Unknown project: {a.slug}")
    entry = {"time": iso(), "kind": a.kind, "text": core.redact(a.text, 4000), "source": a.source, "session_id": a.session}
    append_jsonl(p / "DECISIONS.jsonl" if a.kind == "decision" else p / "EVENTS.jsonl", entry)
    core.git_memory_commit(f"{a.slug}: {a.kind} note")
    print("RECORDED")


# ---------------------------------------------------------------- chat bridge

def cmd_chat_export(a):
    from . import chat
    target, toks = chat.export(a.slug, a.query or "", a.tokens or config()["default_context_tokens"], a.output)
    print(f"CHAT_EXPORT={target}")
    print(f"APPROX_TOKENS={toks}")


def cmd_chat_import(a):
    from . import chat
    r = chat.import_packet(a.slug, a.packet, dry_run=a.dry_run, note=a.note or "", session=a.session)
    print(f"CHAT_IMPORT={r['status']} dry_run={r['dry_run']} sha256={r['sha256']}")
    for c in r["conflicts"]:
        print("CONFLICT:", c)
    for w in r["warnings"]:
        print("WARN:", w)
    for m in r["matches"]:
        print("MATCH:", m)
    if r.get("stored_path"):
        print(f"STORED={r['stored_path']} object={r['object']} dedup={r.get('deduplicated')}")
    print(f"REPORT={r['report']}")
    raise SystemExit(core.EXIT_CONFLICT if r["conflicts"] else 0)


# ---------------------------------------------------------------- health / dashboard / backup

def cmd_health(a):
    from . import health
    h = health.collect(a.slug, check_repo=not a.no_repo)
    if a.json:
        print(json.dumps(h, indent=2, default=str))
    else:
        print(health.render(h, verbose=a.verbose))
    raise SystemExit(h["exit_code"])


def cmd_dashboard(a):
    from . import dashboard
    if a.serve:
        dashboard.serve(int(a.port or config()["dashboard_port"]))
        return
    if a.stop:
        print("DASHBOARD_STOPPED" if dashboard.stop() else "DASHBOARD_NOT_RUNNING")
        return
    if a.status:
        st = dashboard.status()
        print(f"DASHBOARD_RUNNING={st.get('running')} url=http://{st.get('host','127.0.0.1')}:{st.get('port','-')}/ pid={st.get('pid','-')}")
        raise SystemExit(0 if st.get("running") else 1)
    st = dashboard.start(a.port, foreground=a.foreground)
    print(f"DASHBOARD_RUNNING=True url=http://{st['host']}:{st['port']}/ pid={st['pid']}")


def cmd_backup(a):
    from . import backup
    t = backup.create(a.output_dir, a.label or "")
    print(f"BACKUP=PASS {t}")
    if a.verify:
        r = backup.verify(t)
        print(f"BACKUP_VERIFY={r['result']}")
        raise SystemExit(0 if r["result"] == "PASS" else 1)


def cmd_backups(a):
    from . import backup
    rows = backup.list_backups()
    if not rows:
        print("NO_BACKUPS")
        return
    for r in rows:
        print(f"{r['name']}\t{core.human_bytes(r['bytes'])}\tage={r['age_hours']}h\tengine={r['engine_version'] or '?'}\tverified={r['verified'] or 'NO'}\tlabel={r['label'] or '-'}")


def cmd_restore(a):
    from . import backup
    if a.verify:
        r = backup.verify(a.backup, keep_temp=a.keep_temp)
        print(f"RESTORE_VERIFY={r['result']} backup={r['backup']}")
        for k, v in r["checks"]:
            print(f"  {k}: {v}")
        if r["result"] != "PASS" and r.get("doctor_output"):
            print(r["doctor_output"])
        raise SystemExit(0 if r["result"] == "PASS" else 1)
    aside = backup.restore(a.backup, confirm=a.confirm)
    print(f"RESTORE=PASS previous_root_moved_to={aside}")


def cmd_gc(a):
    import time
    core.ensure_root()
    removed = 0
    cutoff = time.time() - (a.days * 86400)
    for p in core.PROJECTS.glob("*/.generated/*"):
        try:
            if p.is_file() and p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    print(f"GC=PASS removed={removed}")


# ---------------------------------------------------------------- integration / migration / selftest

def cmd_integrate(a):
    from . import integrate
    core.ensure_root()
    man = core.project_manifest(a.slug)
    repo_s = a.repo or man.get("repo") or ""
    if not repo_s:
        die("No repo path configured or supplied")
    repo = Path(repo_s).expanduser()
    if not repo.exists():
        die(f"Repo path missing: {repo}")
    memdir = repo / ".ai-memory"
    memdir.mkdir(exist_ok=True)
    atomic_write(memdir / "README.md", f"# Local AI Memory Pointer\n\nProject: `{a.slug}`\nCanonical root: `{ROOT}`\n"
                 f"Current state: `{project_dir(a.slug) / 'CURRENT.md'}`\nProtocol: `{ROOT / 'AI_MEMORY_AGENT_PROTOCOL_V4.md'}`\n\n"
                 "Do not duplicate the full memory archive into this repository.\n")
    print(f"CREATED {memdir / 'README.md'}")
    if a.instructions:
        for fn, agent in (("AGENTS.md", "codex"), ("CLAUDE.md", "claude"), ("GEMINI.md", "gemini")):
            if a.agent != "all" and agent != a.agent:
                continue
            print(f"{fn}: {integrate.patch_file(repo / fn, agent)}")
    else:
        print("INSTRUCTIONS_NOT_MODIFIED (use --instructions to opt in)")


def cmd_integrate_global(a):
    from . import integrate
    for path, agent in integrate.global_targets():
        if a.agent != "all" and agent != a.agent:
            continue
        r = integrate.patch_file(path, agent, dry_run=a.dry_run)
        ok = integrate.verify_file(path, agent) if not a.dry_run else "DRY_RUN"
        print(f"{path}: {r} verified={ok}")


def cmd_migrate(a):
    from . import migrate
    for act in migrate.plan_and_apply(dry_run=a.dry_run):
        print(("PLAN: " if a.dry_run else "APPLIED: ") + act)
    print(f"MIGRATE={'DRY_RUN' if a.dry_run else 'PASS'}")


def cmd_import_handoff(a):
    from . import chat
    r = chat.import_packet(a.slug, a.file, dry_run=False, note=a.note or "", session=a.session)
    print(f"IMPORTED={r.get('stored_path')} status={r['status']}")


def cmd_selftest(a):
    from . import selftest
    rc = selftest.run(full=a.full, keep=a.keep, verbose=a.verbose)
    raise SystemExit(rc)


def cmd_stress(a):
    from . import selftest
    rc = selftest.run_stress_only(sessions=a.sessions, steps=a.steps, keep=a.keep)
    raise SystemExit(rc)


# ---------------------------------------------------------------- parser

def build_parser():
    ap = argparse.ArgumentParser(prog="aimem", description=f"Local AI Memory OS {core.VERSION} (no API, no cloud)")
    sp = ap.add_subparsers(dest="cmd", required=True)

    p = sp.add_parser("version"); p.set_defaults(func=cmd_version)
    p = sp.add_parser("init"); p.add_argument("path", nargs="?"); p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_init)
    p = sp.add_parser("status"); p.set_defaults(func=cmd_status)
    p = sp.add_parser("register"); p.add_argument("slug"); p.add_argument("--name"); p.add_argument("--repo"); p.add_argument("--update", action="store_true"); p.set_defaults(func=cmd_register)
    p = sp.add_parser("doctor"); p.add_argument("--deep", action="store_true"); p.add_argument("--repair", action="store_true"); p.add_argument("--no-repo", action="store_true", help="skip live repo checks"); p.add_argument("--slug"); p.set_defaults(func=cmd_doctor)
    p = sp.add_parser("reindex"); p.add_argument("slug", nargs="?"); p.set_defaults(func=cmd_reindex)
    p = sp.add_parser("search"); p.add_argument("slug", nargs="?"); p.add_argument("query"); p.add_argument("--all", action="store_true"); p.add_argument("--limit", type=int, default=10); p.set_defaults(func=cmd_search)
    p = sp.add_parser("context"); p.add_argument("slug"); p.add_argument("--query", default=""); p.add_argument("--tokens", type=int); p.add_argument("--mode", choices=["hot", "smart", "deep"], default="smart"); p.add_argument("--output"); p.add_argument("--stdout", action="store_true"); p.set_defaults(func=cmd_context)

    p = sp.add_parser("begin"); p.add_argument("slug"); p.add_argument("--agent", choices=AGENTS, required=True); p.add_argument("--task", required=True); p.add_argument("--tokens", type=int); p.add_argument("--mode", choices=["hot", "smart", "deep"], default="smart"); p.set_defaults(func=cmd_begin)
    p = sp.add_parser("heartbeat"); p.add_argument("slug"); p.add_argument("--session"); p.add_argument("--note"); p.set_defaults(func=cmd_heartbeat)
    p = sp.add_parser("finish"); p.add_argument("slug"); p.add_argument("--session"); p.add_argument("--result", required=True); p.add_argument("--label"); p.add_argument("--summary"); p.add_argument("--allow-unchanged", action="store_true"); p.add_argument("--no-advance-current", action="store_true"); p.add_argument("--expect-version", type=int); p.add_argument("--acknowledge-newer", action="store_true", help="finish even though another session advanced memory (you merged it)"); p.set_defaults(func=cmd_finish)
    p = sp.add_parser("checkpoint"); p.add_argument("slug"); p.add_argument("--label", required=True); p.add_argument("--result", required=True); p.add_argument("--summary"); p.add_argument("--session"); p.add_argument("--no-advance-current", action="store_true"); p.add_argument("--expect-version", type=int); p.set_defaults(func=cmd_checkpoint)
    p = sp.add_parser("sessions"); p.add_argument("slug", nargs="?"); p.add_argument("--open", action="store_true"); p.set_defaults(func=cmd_sessions)
    p = sp.add_parser("session"); p.add_argument("action", choices=["close", "show"]); p.add_argument("slug"); p.add_argument("--session"); p.add_argument("--result", default="ABANDONED"); p.add_argument("--note"); p.set_defaults(func=cmd_session)
    p = sp.add_parser("write-current", help="transactional CAS write of CURRENT.md/NEXT.md from files"); p.add_argument("slug"); p.add_argument("--current"); p.add_argument("--next"); p.add_argument("--session"); p.add_argument("--expect-version", type=int); p.add_argument("--allow-secret-pattern", action="store_true"); p.set_defaults(func=cmd_write_current)
    p = sp.add_parser("txn"); p.add_argument("slug", nargs="?"); p.add_argument("--repair", action="store_true"); p.set_defaults(func=cmd_txn)

    p = sp.add_parser("recover"); p.add_argument("slug", nargs="?"); p.add_argument("--session"); p.set_defaults(func=cmd_recover)
    p = sp.add_parser("reconcile"); p.add_argument("slug"); p.set_defaults(func=cmd_reconcile)
    p = sp.add_parser("fact"); p.add_argument("action", choices=["set", "list"]); p.add_argument("slug"); p.add_argument("--key"); p.add_argument("--value"); p.add_argument("--source-type"); p.add_argument("--source-ref"); p.add_argument("--status", default="REPORTED"); p.add_argument("--session"); p.add_argument("--evidence"); p.add_argument("--note"); p.set_defaults(func=cmd_fact)
    p = sp.add_parser("compact"); p.add_argument("slug", nargs="?"); p.add_argument("--all", action="store_true"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--rebuild", action="store_true"); p.set_defaults(func=cmd_compact)
    p = sp.add_parser("objects"); p.add_argument("action", choices=["stat", "verify"], nargs="?", default="stat"); p.set_defaults(func=cmd_objects)

    p = sp.add_parser("capture"); p.add_argument("slug"); p.add_argument("--session"); p.set_defaults(func=cmd_capture)
    p = sp.add_parser("note"); p.add_argument("slug"); p.add_argument("--kind", choices=["decision", "error", "todo", "info", "warning"], required=True); p.add_argument("--text", required=True); p.add_argument("--source", default="manual"); p.add_argument("--session"); p.set_defaults(func=cmd_note)
    p = sp.add_parser("artifact"); p.add_argument("slug"); p.add_argument("--path", required=True); p.add_argument("--kind", default="file"); p.add_argument("--note"); p.add_argument("--copy", action="store_true", help="(legacy) store content-addressed copy"); p.add_argument("--store", action="store_true", help="store content-addressed copy"); p.add_argument("--session"); p.set_defaults(func=cmd_artifact)

    p = sp.add_parser("chat-export"); p.add_argument("slug"); p.add_argument("--query", default=""); p.add_argument("--tokens", type=int); p.add_argument("--output"); p.set_defaults(func=cmd_chat_export)
    p = sp.add_parser("chat-import"); p.add_argument("slug"); p.add_argument("packet"); p.add_argument("--dry-run", action="store_true"); p.add_argument("--note"); p.add_argument("--session"); p.set_defaults(func=cmd_chat_import)
    p = sp.add_parser("import-handoff"); p.add_argument("slug"); p.add_argument("--file", required=True); p.add_argument("--note"); p.add_argument("--session"); p.set_defaults(func=cmd_import_handoff)

    p = sp.add_parser("health"); p.add_argument("slug", nargs="?"); p.add_argument("--json", action="store_true"); p.add_argument("--verbose", "-v", action="store_true"); p.add_argument("--no-repo", action="store_true"); p.set_defaults(func=cmd_health)
    p = sp.add_parser("dashboard"); p.add_argument("--stop", action="store_true"); p.add_argument("--status", action="store_true"); p.add_argument("--port", type=int); p.add_argument("--foreground", action="store_true"); p.add_argument("--serve", action="store_true", help=argparse.SUPPRESS); p.set_defaults(func=cmd_dashboard)
    p = sp.add_parser("backup"); p.add_argument("--output-dir"); p.add_argument("--label"); p.add_argument("--verify", action="store_true"); p.set_defaults(func=cmd_backup)
    p = sp.add_parser("backups"); p.set_defaults(func=cmd_backups)
    p = sp.add_parser("restore"); p.add_argument("backup"); p.add_argument("--verify", action="store_true"); p.add_argument("--confirm", action="store_true"); p.add_argument("--keep-temp", action="store_true"); p.set_defaults(func=cmd_restore)
    p = sp.add_parser("gc"); p.add_argument("--days", type=int, default=14); p.set_defaults(func=cmd_gc)

    p = sp.add_parser("integrate"); p.add_argument("slug"); p.add_argument("--repo"); p.add_argument("--instructions", action="store_true"); p.add_argument("--agent", choices=["all", "claude", "codex", "gemini"], default="all"); p.set_defaults(func=cmd_integrate)
    p = sp.add_parser("integrate-global"); p.add_argument("--agent", choices=["all", "claude", "codex"], default="all"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_integrate_global)
    p = sp.add_parser("migrate"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(func=cmd_migrate)
    p = sp.add_parser("selftest"); p.add_argument("--full", action="store_true"); p.add_argument("--keep", action="store_true"); p.add_argument("--verbose", "-v", action="store_true"); p.set_defaults(func=cmd_selftest)
    p = sp.add_parser("stress"); p.add_argument("--sessions", type=int, default=10); p.add_argument("--steps", type=int, default=20); p.add_argument("--keep", action="store_true"); p.set_defaults(func=cmd_stress)
    from . import handoff
    handoff.add_parser(sp)
    return ap


def main(argv=None):
    ap = build_parser()
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
