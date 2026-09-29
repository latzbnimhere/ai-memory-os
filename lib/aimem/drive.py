"""aimem drive — Google Drive mirror / coordination layer for the EXISTING local AI Memory.

LOCAL_MEMORY_ROOT (core.ROOT) stays the only authoritative memory. Drive holds a verified, bounded mirror that other
agents (ChatGPT, Claude, Codex, ...) can read without the local CLI. Nothing here ever writes canonical local memory
from Drive: pull copies review material into <root>/.drive/pulled/, conflicts are reported, never auto-merged.

Identity: drivefs-item-id-v1 (driveid.py) — Google account + server-assigned Drive folder ids. Never st_dev/st_ino.

Publish protocol (per project, `push`):
  1. local flock drive.<slug> + Drive LEASE.json (single writer; expired leases are stale writers, taken over loudly)
  2. read remote MANIFEST.json; classify against local drive-state: a newer, rewritten, rolled-back or foreign remote
     is a CONFLICT and nothing is written
  3. consistent local snapshot under the project write lock -> staging dir; secret scan (refuse, never redact)
  4. write immutable VERSIONS/vNNNNNN-<gen>/ + top-level files; local readback (sha256 of mount bytes)
  5. remote readback: DriveFS server metadata must show a server file id, the size and the md5 of every file
  6. re-read remote MANIFEST (stale-writer CAS), lease still ours, local canonical unchanged
  7. only then write MANIFEST.json (the commit marker), read it back and wait for its server acknowledgement
A crash or timeout before 7 leaves the previous version current and a pending marker for retry.

Readers: MANIFEST.json names the committed version. Top-level files are valid only when their sha256 equals the
manifest entry; otherwise read VERSIONS/<current_version_dir>/, which is complete before it is ever committed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import core, driveid
from .core import iso, parse_iso

SCHEMA = "aimem-drive-mirror-v1"
ROOT_NAME = "AI-Memory"
MANIFEST = "MANIFEST.json"
LEASE = "LEASE.json"
HOT = ("CURRENT.md", "NEXT.md", "LATEST_HANDOFF.md", "LATEST_CHECKPOINT.json")
JOURNALS = ("DECISIONS.jsonl", "EVENTS.jsonl")
SUBDIRS = ("PROMPTS", "CHECKPOINTS", "EVIDENCE_INDEX", "ARTIFACT_INDEX", "VERSIONS", "INBOX")
DEFAULTS = {"cloud_ack_timeout_s": 300, "finish_ack_timeout_s": 120, "sweep_ack_timeout_s": 60,
            "lease_ttl_s": 1800, "begin_check": True, "finish_publish": True, "handoff_context_tokens": 6000,
            "max_prompt_bytes": 200_000, "max_attach_bytes": 100 * 1024 * 1024}
LAW = ("MIRROR/COORDINATION ONLY. Local AI Memory (LOCAL_MEMORY_ROOT on the owner's Mac) is authoritative; freshly "
       "verified physical repo/runtime state overrides this mirror and local memory. Never mutation authority, never a "
       "replacement for sealed evidence, never permission to continue an irreversible phase.")
READ_ORDER = ["MANIFEST.json", "LATEST_HANDOFF.md", "NEXT.md", "CURRENT.md (search/sections, not whole-archive loads)",
              "LATEST_CHECKPOINT.json", "EVIDENCE_INDEX/INDEX.json and ARTIFACT_INDEX/INDEX.json only when needed"]
HOT_RULE = ("Load only MANIFEST.json + LATEST_HANDOFF.md by default. Never recursively load CHECKPOINTS/, VERSIONS/, "
            "EVIDENCE_INDEX/, ARTIFACT_INDEX/ or journals into context; fetch single files by name when a task needs them.")
PROPOSAL_RULE = ("Agents without the local aimem CLI never overwrite mirror files: they write one proposal file to "
                 "INBOX/<UTC>__<agent>__<topic>.md. A local agent reconciles it against physical state and local memory.")


class DriveError(Exception):
    def __init__(self, code, detail="", exit_code=core.EXIT_ERROR):
        self.code, self.detail, self.exit_code = code, detail, exit_code
        super().__init__(code + (": " + detail if detail else ""))


# ------------------------------------------------------------------ secret policy (refuse, never redact-and-publish)

EXTRA_SECRET_PATTERNS = [
    ("otpauth URI", re.compile(r"(?i)otpauth://")),
    ("Credential URL", re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,20}://[^\s/:@'\"<>]+:[^\s/@'\"<>]+@[^\s/]")),
    ("Secret assignment", re.compile(
        r"(?i)\b(?:totp[_ -]?secret|mfa[_ -]?secret|2fa[_ -]?secret|recovery[_ -]?codes?|backup[_ -]?codes?|"
        r"client[_ -]?secret|private[_ -]?key|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|auth[_ -]?token|"
        r"security[_ -]?answer|secret[_ -]?answer|db[_ -]?password|app[_ -]?specific[_ -]?password)"
        r"\s*[\"']?\s*[:=]\s*[\"']?(?!\[REDACTED)(?=[A-Za-z0-9+/_\-]*\d)(?=[A-Za-z0-9+/_\-]*[A-Za-z])[A-Za-z0-9+/_\-]{12,}")),
    ("OTP code", re.compile(r"(?i)\b(?:otp|one[- ]time (?:pass(?:word|code)?|code)|verification code|2fa code|sms code)"
                            r"\s*(?:is|=|:)\s*\d{4,8}\b")),
]
IBAN_RX = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?)\b")
CARD_RX = re.compile(r"(?<![\w.-])((?:\d[ -]?){12,18}\d)(?![\w.-])")


def _iban_ok(s):
    s = s.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    return int("".join(str(int(c, 36)) for c in s[4:] + s[:4])) % 97 == 1


def _luhn(d):
    t = 0
    for i, c in enumerate(reversed(d)):
        x = int(c)
        if i % 2:
            x *= 2
            x -= 9 if x > 9 else 0
        t += x
    return t % 10 == 0


def secret_hits(text):
    """Labels only — values are never echoed or stored."""
    text = text or ""
    hits = core.secret_hits_text(text) + [label for label, rx in EXTRA_SECRET_PATTERNS if rx.search(text)]
    if any(_iban_ok(m.group(1)) for m in IBAN_RX.finditer(text)):
        hits.append("IBAN")
    for m in CARD_RX.finditer(text):
        d = re.sub(r"[ -]", "", m.group(1))
        if 13 <= len(d) <= 19 and d[0] in "3456" and len(set(d)) > 1 and _luhn(d):
            hits.append("Payment card")
            break
    return sorted(set(hits))


# ------------------------------------------------------------------ small helpers

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha256_path(p):
    return core.sha256_file(Path(p))


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def reg_path():
    return core.ROOT / "registry" / "drive-mirror.json"


def state_path(slug):
    return core.ROOT / "registry" / "drive-state" / f"{core.safe_component(slug, 'slug')}.json"


def work():
    p = core.ROOT / ".drive"
    p.mkdir(mode=0o700, exist_ok=True)
    return p


def pending_path(slug):
    d = work() / "pending"
    d.mkdir(mode=0o700, exist_ok=True)
    return d / f"{core.safe_component(slug, 'slug')}.json"


def load_reg(required=True):
    p = reg_path()
    if not p.exists():
        if required:
            raise DriveError("DRIVE_MIRROR_NOT_CONFIGURED", "run: aimem drive pin")
        return None
    r = core.load_json(p, None)
    if not isinstance(r, dict) or r.get("version") != 1 or r.get("identity_scheme") != driveid.SCHEME \
            or not isinstance(r.get("root"), dict) or not isinstance(r.get("projects"), dict) or not r.get("writer_root_id"):
        raise DriveError("INVALID_DRIVE_REGISTRY")
    r["settings"] = {**DEFAULTS, **(r.get("settings") or {})}
    return r


def save_reg(r):
    out = dict(r)
    out["settings"] = {k: v for k, v in (r.get("settings") or {}).items()}
    core.atomic_write_json(reg_path(), out)


def load_state(slug):
    return core.try_load_json(state_path(slug), None)


def save_state(slug, st):
    state_path(slug).parent.mkdir(parents=True, exist_ok=True)
    core.atomic_write_json(state_path(slug), st)


def mount_write(path, data: bytes):
    """Same-directory temp + fsync + atomic rename on the DriveFS mount (DriveFS keeps the file id across renames)."""
    path = Path(path)
    if path.is_symlink():
        raise DriveError("SYMLINK_REFUSED", str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def holder(agent, session):
    return {"agent": agent or "unknown", "session": session, "host": socket.gethostname(), "pid": os.getpid(),
            "root_id": None}


# ------------------------------------------------------------------ identity / paths

def root_path(reg):
    exp = reg["root"]
    try:
        return driveid.check_folder(Path(exp["path"]), exp, name=ROOT_NAME)
    except driveid.DriveIdentityError as e:
        raise DriveError(e.code, e.detail) from None


def project_entry(reg, slug):
    e = reg["projects"].get(slug)
    if not e or not e.get("enabled", True):
        raise DriveError("PROJECT_NOT_REGISTERED", f"run: aimem drive register {slug}")
    return e


def project_path(reg, slug, root=None):
    root = root or root_path(reg)
    e = project_entry(reg, slug)
    exp = {"identity_scheme": driveid.SCHEME, "account": reg["root"]["account"], "item_id": e["item_id"],
           "my_drive_item_id": reg["root"]["my_drive_item_id"]}
    p = root / e["folder"]
    try:
        driveid.check_folder(p, exp, name=e["folder"])
    except driveid.DriveIdentityError as e2:
        raise DriveError(e2.code, "project folder: " + e2.detail) from None
    if p.parent != root:
        raise DriveError("DRIVE_IDENTITY_MISMATCH", "project folder not directly below root")
    return p


def discover_root(account=None):
    base = driveid.cloudstorage_dir()
    pat = f"GoogleDrive-{account}/My Drive/{ROOT_NAME}" if account else f"GoogleDrive-*/My Drive/{ROOT_NAME}"
    cands = sorted(p for p in base.glob(pat) if p.is_dir() and not p.is_symlink())
    if len(cands) != 1:
        raise DriveError("DRIVE_ROOT_MISSING" if not cands else "DRIVE_ROOT_AMBIGUOUS",
                         f"expected exactly one {pat} under {base}")
    return cands[0]


# ------------------------------------------------------------------ remote reads

def read_manifest(pdir):
    f = pdir / MANIFEST
    if not f.exists():
        return None, None
    b = f.read_bytes()
    if len(b) > 5_000_000:
        raise DriveError("REMOTE_MANIFEST_MALFORMED", "too large", core.EXIT_CONFLICT)
    try:
        m = json.loads(b)
    except ValueError:
        raise DriveError("REMOTE_MANIFEST_MALFORMED", "not JSON", core.EXIT_CONFLICT) from None
    if not isinstance(m, dict) or m.get("schema") != SCHEMA or not isinstance(m.get("version"), int) \
            or not isinstance(m.get("files"), dict):
        raise DriveError("REMOTE_MANIFEST_MALFORMED", "schema", core.EXIT_CONFLICT)
    return m, sha256_bytes(b)


def read_lease(pdir):
    f = pdir / LEASE
    if not f.exists():
        return None
    try:
        j = json.loads(f.read_bytes())
        return j if isinstance(j, dict) else {"state": "MALFORMED"}
    except ValueError:
        return {"state": "MALFORMED"}


def inbox_items(pdir):
    d = pdir / "INBOX"
    if not d.is_dir():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.startswith("."))


# ------------------------------------------------------------------ lease (single writer)

def lease_live(lease):
    if not lease or lease.get("state") != "HELD":
        return False
    exp = parse_iso(lease.get("expires_at"))
    return bool(exp and exp > core.now())


def acquire_lease(pdir, who, base_version, ttl, notes):
    cur = read_lease(pdir)
    same = bool(cur and (cur.get("holder") or {}).get("root_id") == who["root_id"]
                and who.get("session") and (cur.get("holder") or {}).get("session") == who.get("session"))
    if cur and cur.get("state") == "MALFORMED":
        raise DriveError("LEASE_MALFORMED", "inspect LEASE.json; never overwritten automatically", core.EXIT_CONFLICT)
    if lease_live(cur) and not same:
        h = cur.get("holder") or {}
        raise DriveError("LEASE_HELD", f"agent={h.get('agent')} session={h.get('session')} host={h.get('host')} "
                                       f"expires_at={cur.get('expires_at')}", core.EXIT_CONFLICT)
    prev = None
    if cur and cur.get("state") == "HELD":
        prev = {k: cur.get(k) for k in ("lease_id", "holder", "acquired_at", "expires_at", "base_version")}
        if not same:
            notes.append(f"STALE_WRITER_LEASE_TAKEN_OVER holder={(cur.get('holder') or {}).get('agent')}"
                         f"/{(cur.get('holder') or {}).get('session')} expired_at={cur.get('expires_at')}")
    now = core.now()
    lease = {"schema": SCHEMA, "state": "HELD", "lease_id": uuid.uuid4().hex, "holder": who,
             "acquired_at": now.isoformat(timespec="seconds"),
             "expires_at": (now + dt.timedelta(seconds=int(ttl))).isoformat(timespec="seconds"),
             "base_version": base_version, "took_over": prev,
             "rule": "Single writer. Do not write MANIFEST.json or mirror files while another live lease is HELD."}
    mount_write(pdir / LEASE, dumps(lease).encode())
    back = read_lease(pdir)
    if not back or back.get("lease_id") != lease["lease_id"]:
        raise DriveError("LEASE_RACE_LOST", "another writer replaced LEASE.json", core.EXIT_CONFLICT)
    return lease


def release_lease(pdir, lease, version=None):
    try:
        cur = read_lease(pdir)
        if not cur or cur.get("lease_id") != lease["lease_id"]:
            return False
        rel = {"schema": SCHEMA, "state": "RELEASED", "lease_id": lease["lease_id"], "holder": lease["holder"],
               "acquired_at": lease["acquired_at"], "released_at": iso(), "committed_version": version}
        mount_write(pdir / LEASE, dumps(rel).encode())
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ local snapshot

def _checkpoint_meta(p, cp):
    if not cp or Path(cp).name != cp:
        return None
    return core.try_load_json(p / "checkpoints" / cp / "meta.json", None)


def _latest_facts(slug, limit=120):
    rows = core.read_jsonl(core.project_dir(slug) / "PROVENANCE.jsonl")
    latest = {}
    for r in rows:
        if isinstance(r, dict) and r.get("key"):
            latest[r["key"]] = {k: r.get(k) for k in ("value", "status", "source_type", "source_ref", "evidence_sha256",
                                                     "session", "time", "verified_at")}
    keys = sorted(latest)[-limit:]
    return {k: latest[k] for k in keys}


def _evidence_steps(events, limit=80):
    out = []
    for r in events:
        if r.get("kind") == "operational_step" and r.get("step_kind") in ("command", "test", "error", "result", "checkpoint", "write"):
            out.append({k: r.get(k) for k in ("time", "session_id", "agent", "step_kind", "summary", "result", "command",
                                              "exit_code", "files")})
        elif r.get("kind") in ("error", "checkpoint"):
            out.append({"time": r.get("time"), "kind": r.get("kind"), "text": r.get("text"), "id": r.get("id"),
                        "result": r.get("result"), "current_sha256": r.get("current_sha256")})
    return out[-limit:]


def _prompt_sources(slug, settings):
    """Continuation prompts/instructions: local projects/<slug>/prompts/ plus text files of the legacy handoff folder.
    Google Docs pointers (.gdoc) are referenced by doc id, never copied. Returns ({name: bytes}, [refs])."""
    files, refs = {}, []
    cap = int(settings["max_prompt_bytes"])
    local = core.project_dir(slug) / "prompts"
    if local.is_dir():
        for f in sorted(local.iterdir()):
            if f.is_file() and f.suffix.lower() in (".md", ".txt") and f.stat().st_size <= cap:
                files["local__" + f.name] = f.read_bytes()
    try:
        from . import handoff
        c = handoff.config()
        pe = next((x for x in c["projects"] if x.get("memory_slug") == slug), None)
        if pe:
            hroot = handoff.drive(c)
            hdir = hroot / pe["folder"]
            for f in sorted(hdir.rglob("*")):
                if not f.is_file() or f.name.startswith(".") or f.name in handoff.NAMES:
                    continue
                rel = f.relative_to(hdir).as_posix()
                if f.suffix.lower() == ".gdoc":
                    j = core.try_load_json(f, {}) or {}
                    if driveid.valid_item_id(j.get("doc_id", "")):
                        refs.append({"title": rel[:-5], "google_doc_id": j["doc_id"],
                                     "url": f"https://docs.google.com/document/d/{j['doc_id']}",
                                     "source": "legacy-handoff:" + pe["folder"]})
                elif f.suffix.lower() in (".md", ".txt") and f.stat().st_size <= cap:
                    files["legacy_handoff__" + rel.replace("/", "__")] = f.read_bytes()
            for n in handoff.NAMES:
                if (hdir / n).is_file():
                    refs.append({"title": "bridge " + n, "path": f"AI-Project-Handoffs/{pe['folder']}/{n}",
                                 "sha256": sha256_path(hdir / n), "source": "handoff-bridge-R1"})
    except (Exception, SystemExit):
        pass  # the legacy bridge is optional context; its absence never blocks the mirror
    return files, refs


def render_protocol():
    return f"""# AI Memory Drive mirror — agent protocol ({SCHEMA})

{LAW}

## Read (any agent: ChatGPT, Claude, Codex, other)
1. Open `<slug>/MANIFEST.json`. `version` + `generation` identify the committed state; `current_version_dir` names
   an immutable, complete copy of the hot files.
2. Read `<slug>/LATEST_HANDOFF.md`. Its `DRIVE_VERSION` and `GENERATION` lines must equal MANIFEST.json. If they differ,
   or if you can compute SHA-256 and a top-level file does not match `files.<name>.sha256`, a publish is in progress:
   read the same file names from `<slug>/<current_version_dir>/` instead (or retry after ~60 s).
3. Then `NEXT.md`, then only the `CURRENT.md` sections you need. {HOT_RULE}
4. Treat everything as REPORTED until the physical repo/runtime is freshly verified. `physical` in MANIFEST.json is the
   state at publish time, not now. Fresh physical state overrides this mirror and local memory.
5. Never ask for, store or repeat passwords, OTPs, API keys, banking numbers, security answers or other secrets.

## Write
- Agents WITH the local CLI (on the owner's Mac): `aimem drive push <slug>` (or `aimem finish`, which publishes and
  verifies automatically). Never edit mirror files by hand.
- Agents WITHOUT the local CLI (e.g. ChatGPT through a Drive connector): {PROPOSAL_RULE} Include: what you verified
  (with commands/results/hashes), what is only reported, the MANIFEST version you read, and no secrets and no hidden
  reasoning. Never edit MANIFEST.json, LEASE.json, CURRENT.md, NEXT.md or anything under VERSIONS/ or CHECKPOINTS/.
- `LEASE.json` with `state: HELD` and a future `expires_at` means a writer is active; wait. An expired HELD lease is a
  stale writer; only the local CLI may take it over.

## Integrity
- MANIFEST.json is written last and only after every other file was read back from the mount AND acknowledged by the
  Drive server (server file id + md5). `files.<name>.drive_id` are stable Google Drive file ids.
- Versions only increase. A local publisher refuses to overwrite a newer, rewritten, rolled-back or foreign manifest.
"""


def render_continue(slug, name):
    return f"""# Continue {name} ({slug}) from the Drive mirror

Paste to any agent that can read this Google Drive folder:

> Read `AI-Memory/{slug}/MANIFEST.json`, then `AI-Memory/{slug}/LATEST_HANDOFF.md` (check that DRIVE_VERSION and
> GENERATION match the manifest, else use the manifest's current_version_dir), then `NEXT.md`. Load CURRENT.md only
> section by section. Do not load CHECKPOINTS/, VERSIONS/, journals or indexes wholesale. Treat all physical claims as
> REPORTED until verified on the real repo/runtime; fresh physical state wins. Follow
> `AI-Memory/{slug}/PROMPTS/DRIVE_AGENT_PROTOCOL.md`. If you cannot run the local aimem CLI, write updates only as a
> proposal file in `AI-Memory/{slug}/INBOX/`. Never include secrets or hidden reasoning.
"""


def _handoff_section(slug):
    try:
        from . import handoff
        c = handoff.config()
        pe = next((x for x in c["projects"] if x.get("memory_slug") == slug), None)
        if not pe:
            return ""
        return handoff.build(pe)[handoff.NAMES[0]]
    except (Exception, SystemExit):
        return ""


def resolve_mode(slug, mode="auto", session=None):
    """auto: live canonical CURRENT/NEXT, unless ANOTHER active session has uncheckpointed edits in progress — then the
    accepted (immutable) current checkpoint is published and the pending edits are flagged, never mirrored half-done."""
    if mode in ("canonical", "checkpoint"):
        return mode, []
    from . import sessions
    p = core.project_dir(slug)
    meta = _checkpoint_meta(p, core.project_manifest(slug).get("current_checkpoint")) or {}
    nxt = core.sha256_file(p / "NEXT.md") if (p / "NEXT.md").exists() else sha256_bytes(b"")
    if not meta or (core.sha256_file(p / "CURRENT.md") == meta.get("current_sha256")
                    and nxt == (meta.get("next_sha256") or sha256_bytes(b""))):
        return "canonical", []
    others = [j.get("id") for _f, j in sessions.open_sessions(slug)
              if j.get("id") != session and sessions.lease_state(j) == "ACTIVE"]
    if others:
        return "checkpoint", [f"UNCHECKPOINTED_LOCAL_EDITS_NOT_PUBLISHED active_session={','.join(others)}"]
    return "canonical", []


def snapshot(slug, reg, version, generation, backfill=0, mode="canonical"):
    """Consistent copy of local memory into a private staging dir. Returns (stage, info).
    mode canonical: live CURRENT/NEXT; mode checkpoint: the current checkpoint's sealed CURRENT/NEXT."""
    from . import context as ctxmod, reconcile
    p = core.project_dir(slug)
    settings = reg["settings"]
    live = core.repo_state_for(slug)  # fresh physical state (read-only git), outside the lock
    rec = reconcile.reconcile(slug, live)
    stage = Path(tempfile.mkdtemp(prefix=f"stage-{slug}-", dir=str(work())))
    files = {}

    def put(rel, data, role):
        if isinstance(data, str):
            data = data.encode()
        f = stage / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        files[rel] = {"role": role, "sha256": sha256_bytes(data), "md5": hashlib.md5(data).hexdigest(), "bytes": len(data)}

    with core.project_write_lock(slug):
        man = core.project_manifest(slug)
        live_cur = (p / "CURRENT.md").read_bytes()
        live_nxt = (p / "NEXT.md").read_bytes() if (p / "NEXT.md").exists() else b""
        cur_b, nxt_b = live_cur, live_nxt
        if mode == "checkpoint":
            cpd0 = p / "checkpoints" / str(man.get("current_checkpoint"))
            m0 = _checkpoint_meta(p, man.get("current_checkpoint")) or {}
            if not m0 or not (cpd0 / "CURRENT.md").is_file():
                raise DriveError("NO_ACCEPTED_CHECKPOINT", "checkpoint mode needs a current checkpoint")
            cur_b = (cpd0 / "CURRENT.md").read_bytes()
            nxt_b = (cpd0 / "NEXT.md").read_bytes() if (cpd0 / "NEXT.md").is_file() else b""
            if sha256_bytes(cur_b) != m0.get("current_sha256"):
                raise DriveError("CHECKPOINT_INTEGRITY_FAILED", str(man.get("current_checkpoint")))
        put("CURRENT.md", cur_b, "hot")
        put("NEXT.md", nxt_b, "hot")
        for j in JOURNALS:
            if (p / j).exists():
                put(j, (p / j).read_bytes(), "journal")
        if (p / "PROVENANCE.jsonl").exists():
            put("EVIDENCE_INDEX/PROVENANCE.jsonl", (p / "PROVENANCE.jsonl").read_bytes(), "index")
        if (p / "ARTIFACTS.jsonl").exists():
            put("ARTIFACT_INDEX/ARTIFACTS.jsonl", (p / "ARTIFACTS.jsonl").read_bytes(), "index")
        cp_id = man.get("current_checkpoint")
        meta = _checkpoint_meta(p, cp_id) or {}
        # authoritative checkpoints: current + latest admin + the one the handoff state is bound to (+ backfill)
        wanted = [cp_id] if cp_id else []
        facts = _latest_facts(slug, limit=10_000)
        admin = (facts.get("checkpoint.admin") or {}).get("value")
        if admin:
            wanted.append(admin)
        all_ids = sorted(d.name for d in (p / "checkpoints").iterdir() if d.is_dir()) if (p / "checkpoints").is_dir() else []
        if backfill:
            wanted += all_ids[-int(backfill):]
        mirrored = []
        for cid in dict.fromkeys(wanted):
            cm = _checkpoint_meta(p, cid)
            if not cm:
                continue
            cpd = p / "checkpoints" / cid
            for n in ("meta.json", "CURRENT.md", "NEXT.md", "REPO_STATE.json"):
                if (cpd / n).is_file():
                    put(f"CHECKPOINTS/{cid}/{n}", (cpd / n).read_bytes(), "checkpoint")
            mirrored.append(cid)
        index_rows = []
        for cid in all_ids:
            cm = _checkpoint_meta(p, cid) or {}
            index_rows.append({"id": cid, "time": cm.get("time"), "label": cm.get("label"), "result": cm.get("result"),
                               "memory_version": cm.get("memory_version"), "advances_current": cm.get("advances_current_checkpoint"),
                               "session_id": cm.get("session_id"), "current_sha256": cm.get("current_sha256"),
                               "next_sha256": cm.get("next_sha256"), "repo_head": (cm.get("repo_state") or {}).get("head"),
                               "has_meta": bool(cm), "is_current": cid == cp_id,
                               "mirrored_dir": f"CHECKPOINTS/{cid}/" if cid in mirrored else None})
        events = core.read_jsonl(p / "EVENTS.jsonl")
        artifacts = core.read_jsonl(p / "ARTIFACTS.jsonl")
        man_after = core.project_manifest(slug)
    cur_sha, nxt_sha = sha256_bytes(cur_b), sha256_bytes(nxt_b)
    cp_integrity = None
    if meta:
        cpd = p / "checkpoints" / cp_id
        cp_integrity = (core.sha256_file(cpd / "CURRENT.md") == meta.get("current_sha256") and
                        (not meta.get("next_sha256") or core.sha256_file(cpd / "NEXT.md") == meta.get("next_sha256")))
    source = {"memory_version": int(man_after.get("memory_version", 0)), "current_checkpoint": cp_id,
              "checkpoint_result": meta.get("result"), "checkpoint_label": meta.get("label"),
              "checkpoint_time": meta.get("time"), "current_sha256": cur_sha, "next_sha256": nxt_sha,
              "canonical_matches_checkpoint": bool(meta) and cur_sha == meta.get("current_sha256")
              and (not meta.get("next_sha256") or nxt_sha == meta.get("next_sha256")),
              "checkpoint_integrity": cp_integrity, "engine_version": core.VERSION, "mode": mode,
              "live_current_sha256": sha256_bytes(live_cur), "live_next_sha256": sha256_bytes(live_nxt),
              "uncheckpointed_local_edits": (sha256_bytes(live_cur), sha256_bytes(live_nxt)) != (cur_sha, nxt_sha)
              or (bool(meta) and cur_sha != meta.get("current_sha256")),
              "local_memory_path": str(p), "last_writer_session": man_after.get("last_writer_session")}
    physical = {k: live.get(k) for k in ("configured", "path", "exists", "is_git_repo", "branch", "head", "tree", "dirty",
                                        "captured_at")}
    put("LATEST_CHECKPOINT.json", dumps({"schema": SCHEMA, "slug": slug, "checkpoint_id": cp_id, "meta": meta,
                                         "integrity_verified": cp_integrity,
                                         "canonical_matches_checkpoint": source["canonical_matches_checkpoint"],
                                         "mirrored_dir": f"CHECKPOINTS/{cp_id}/" if cp_id in mirrored else None}), "hot")
    put("CHECKPOINTS/INDEX.jsonl", "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in index_rows), "index")
    put("EVIDENCE_INDEX/INDEX.json", dumps({
        "schema": SCHEMA, "slug": slug, "latest_facts": _latest_facts(slug),
        "recent_evidence_steps": _evidence_steps(events), "full_provenance": "EVIDENCE_INDEX/PROVENANCE.jsonl",
        "cold_rule": "Large evidence stays local (paths/hashes here) or in EVIDENCE_INDEX/cold/ via aimem drive attach."}), "index")
    attachments = (load_state(slug) or {}).get("attachments", [])
    put("ARTIFACT_INDEX/INDEX.json", dumps({
        "schema": SCHEMA, "slug": slug, "artifact_count": len(artifacts),
        "artifact_total_bytes": sum(int(a.get("bytes") or 0) for a in artifacts if isinstance(a, dict)),
        "recent_artifacts": [{k: a.get(k) for k in ("name", "kind", "sha256", "bytes", "source_path", "note", "session_id")}
                             for a in artifacts[-40:] if isinstance(a, dict)],
        "cold_attachments": attachments, "full_index": "ARTIFACT_INDEX/ARTIFACTS.jsonl",
        "cold_rule": "Binary artifacts are never uploaded by default; the index keeps name, sha256, bytes and local path."}), "index")
    pfiles, prefs = _prompt_sources(slug, settings)
    put("PROMPTS/DRIVE_AGENT_PROTOCOL.md", render_protocol(), "prompt")
    put("PROMPTS/CONTINUE.md", render_continue(slug, man_after.get("name", slug)), "prompt")
    for n, b in pfiles.items():
        put("PROMPTS/" + n, b, "prompt")
    put("PROMPTS/INDEX.json", dumps({"schema": SCHEMA, "files": sorted(["DRIVE_AGENT_PROTOCOL.md", "CONTINUE.md", *pfiles]),
                                     "references": prefs,
                                     "rule": "Operational prompts/instructions only; never hidden reasoning or secrets."}), "prompt")
    ctx = ctxmod.build_context(slug, "", int(settings["handoff_context_tokens"]), "deep", reconciliation=rec, live_repo=live)
    bridge = _handoff_section(slug)
    handoff_md = "\n".join([
        f"# {man_after.get('name', slug)} — LATEST HANDOFF (AI Memory Drive mirror)", "",
        f"DRIVE_VERSION: {version}", f"GENERATION: {generation}", f"PUBLISHED_AT: {iso()}",
        f"SOURCE_MEMORY_VERSION: {source['memory_version']}",
        f"CHECKPOINT: {cp_id} result={source['checkpoint_result']} time={source['checkpoint_time']}",
        f"CURRENT_SHA256: {cur_sha}", f"NEXT_SHA256: {nxt_sha}",
        f"SOURCE_MODE: {mode}" + (" (accepted checkpoint; another active session has uncheckpointed edits in progress)"
                                  if mode == "checkpoint" else " (live canonical CURRENT/NEXT)"),
        f"PUBLISHED_MATCHES_CHECKPOINT: {'YES' if source['canonical_matches_checkpoint'] else 'NO (uncheckpointed local edits)'}",
        f"PHYSICAL_AT_PUBLISH: head={physical.get('head')} branch={physical.get('branch')} dirty={physical.get('dirty')} "
        f"captured_at={physical.get('captured_at')}",
        f"RECONCILIATION_AT_PUBLISH: {rec['status']}" + (f" flags={','.join(rec['flags'])}" if rec["flags"] else ""), "",
        LAW, "", "READ ORDER: " + " -> ".join(READ_ORDER), HOT_RULE, PROPOSAL_RULE, "",
        "## Navigation handoff (bridge R1 render from the same local sources)" if bridge else "",
        bridge.strip() if bridge else "", "",
        f"## Bounded context pack (engine priority budget, ~{settings['handoff_context_tokens']} tokens)", "", ctx.strip(), ""])
    put("LATEST_HANDOFF.md", handoff_md, "hot")
    info = {"source": source, "physical": physical, "reconciliation": {"status": rec["status"], "flags": rec["flags"]},
            "files": files, "checkpoints_mirrored": mirrored, "memory_version_before": int(man.get("memory_version", 0)),
            "prompt_references": prefs}
    if info["memory_version_before"] != source["memory_version"]:
        shutil.rmtree(stage, ignore_errors=True)
        raise DriveError("LOCAL_CHANGED_DURING_SNAPSHOT", "retry", core.EXIT_WARN)
    return stage, info


def scan_stage(stage, files, allow=False):
    bad = []
    for rel in files:
        hits = secret_hits((stage / rel).read_bytes().decode("utf-8", errors="ignore"))
        if hits:
            bad.append(f"{rel}:{'+'.join(hits)}")
    if bad and not allow:
        raise DriveError("REFUSED_SECRET", "; ".join(bad[:10]) + " (remove the value from local memory; nothing was published)",
                         core.EXIT_CONFLICT)
    return bad


# ------------------------------------------------------------------ conflict classification

def classify(remote, remote_sha, st, source, root_id):
    """Returns list of notes, or raises DriveError(EXIT_CONFLICT). Never lets an older writer replace a newer mirror."""
    notes = []
    if remote is None:
        if st and st.get("version"):
            raise DriveError("REMOTE_MANIFEST_MISSING", f"local state says v{st['version']} was committed; "
                             "inspect the Drive folder (aimem drive reconcile)", core.EXIT_CONFLICT)
        return notes
    rw = remote.get("writer") or {}
    rs = remote.get("source") or {}
    known = bool(st and st.get("version") == remote["version"] and st.get("manifest_sha256") == remote_sha)
    if known:
        return notes
    if st and st.get("version"):
        if remote["version"] < st["version"]:
            raise DriveError("REMOTE_ROLLED_BACK", f"remote v{remote['version']} < last committed v{st['version']}", core.EXIT_CONFLICT)
        if remote["version"] == st["version"]:
            raise DriveError("REMOTE_REWRITTEN", f"remote v{remote['version']} manifest changed outside the protocol", core.EXIT_CONFLICT)
    if rw.get("root_id") != root_id:
        raise DriveError("CONFLICT_FOREIGN_WRITER", f"remote v{remote['version']} written by another memory root "
                         f"({rw.get('host')}); reconcile manually", core.EXIT_CONFLICT)
    rv, lv = int(rs.get("memory_version") or 0), int(source["memory_version"])
    if rv > lv:
        raise DriveError("REMOTE_NEWER", f"remote source memory_version {rv} > local {lv}; never overwritten", core.EXIT_CONFLICT)
    if rv == lv and rs.get("current_checkpoint") != source["current_checkpoint"]:
        raise DriveError("DIVERGED", "same memory_version, different checkpoint", core.EXIT_CONFLICT)
    notes.append(f"REMOTE_UNKNOWN_TO_LOCAL_STATE_SUPERSEDED remote_v={remote['version']} remote_memory_version={rv} local={lv}")
    return notes


# ------------------------------------------------------------------ push

def _files_ok_on_mount(pdir, stage, rels):
    bad = []
    for rel in rels:
        f = pdir / rel
        if not f.is_file() or sha256_path(f) != sha256_path(stage / rel):
            bad.append(rel)
    return bad


def push(slug, agent="unknown", session=None, timeout=None, wait=True, allow_secret=False, backfill=0, reason="manual",
         out=print, mode="auto"):
    reg = load_reg()
    settings = reg["settings"]
    timeout = settings["cloud_ack_timeout_s"] if timeout is None else timeout
    notes = []
    with core.lock(f"drive.{slug}", timeout=120):
        root = root_path(reg)
        pdir = project_path(reg, slug, root)
        e = project_entry(reg, slug)
        for d in SUBDIRS:
            (pdir / d).mkdir(exist_ok=True)
        remote, remote_sha = read_manifest(pdir)
        st = load_state(slug)
        man = core.project_manifest(slug)
        pre_source = {"memory_version": int(man.get("memory_version", 0)), "current_checkpoint": man.get("current_checkpoint")}
        notes += classify(remote, remote_sha, st, pre_source, reg["writer_root_id"])
        who = holder(agent, session)
        who["root_id"] = reg["writer_root_id"]
        base_version = remote["version"] if remote else 0
        version = base_version + 1
        generation = uuid.uuid4().hex
        lease = acquire_lease(pdir, who, base_version, settings["lease_ttl_s"], notes)
        stage = None
        committed = False
        try:
            mode, mnotes = resolve_mode(slug, mode, session)
            notes += mnotes
            stage, info = snapshot(slug, reg, version, generation, backfill, mode)
            src = info["source"]
            if src["memory_version"] != pre_source["memory_version"]:
                notes += classify(remote, remote_sha, st, src, reg["writer_root_id"])
            files = info["files"]
            skey = f"{src['memory_version']}:{src['current_sha256']}:{src['next_sha256']}"
            generation = _reuse_pending_version(slug, pdir, stage, files, base_version, skey) or generation
            scan_stage(stage, files, allow_secret)
            rsrc = (remote or {}).get("source") or {}
            if remote and st and st.get("status") == "VERIFIED" and st.get("manifest_sha256") == remote_sha \
                    and all(rsrc.get(k) == src[k] for k in ("current_sha256", "next_sha256", "memory_version")) \
                    and not _files_ok_on_mount(pdir, stage, ["CURRENT.md", "NEXT.md"]):
                release_lease(pdir, lease, base_version)
                return {"status": "UP_TO_DATE", "version": base_version, "notes": notes}
            vdir = f"VERSIONS/v{version:06d}-{generation[:8]}"
            (stage / vdir).mkdir(parents=True)
            for n in HOT:
                shutil.copyfile(stage / n, stage / vdir / n)
                files[f"{vdir}/{n}"] = {**files[n], "role": "version"}
            prev_vj = core.try_load_json(pdir / vdir / "VERSION.json", {}) or {}
            vjson = dumps({"schema": SCHEMA, "slug": slug, "version": version, "generation": generation,
                           "parent_version": base_version,
                           "created_at": prev_vj.get("created_at") if prev_vj.get("generation") == generation else iso(),
                           "source": src,
                           "files": {n: {k: files[n][k] for k in ("sha256", "md5", "bytes")} for n in HOT}}).encode()
            (stage / vdir / "VERSION.json").write_bytes(vjson)
            files[f"{vdir}/VERSION.json"] = {"role": "version", "sha256": sha256_bytes(vjson),
                                              "md5": hashlib.md5(vjson).hexdigest(), "bytes": len(vjson)}
            # Anything that differs from the last committed manifest without us writing it is preserved, never lost.
            if remote:
                for rel, ent in remote["files"].items():
                    f = pdir / rel
                    if rel in files and f.is_file() and sha256_path(f) != ent.get("sha256") \
                            and sha256_path(f) != files[rel]["sha256"]:
                        q = pdir / "INBOX" / f"QUARANTINE_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}__{rel.replace('/', '__')}"
                        mount_write(q, f.read_bytes())
                        notes.append(f"REMOTE_FILE_MODIFIED_OUTSIDE_PROTOCOL {rel} preserved as INBOX/{q.name}")
            # 4. write content (version dir first), skipping files whose mount bytes are already identical
            order = sorted(files, key=lambda r: (not r.startswith("VERSIONS/"), r))
            written = []
            for rel in order:
                dst = pdir / rel
                if dst.is_file() and dst.stat().st_size == files[rel]["bytes"] and sha256_path(dst) == files[rel]["sha256"]:
                    continue
                mount_write(dst, (stage / rel).read_bytes())
                written.append(rel)
            bad = _files_ok_on_mount(pdir, stage, files)
            if bad:
                raise DriveError("MOUNT_READBACK_FAILED", ", ".join(bad[:5]))
            # 5. remote readback of every content file (written or already present)
            if not wait:
                _mark_pending(slug, "CONTENT_WRITTEN_NOT_COMMITTED", version, reason,
                              {"generation": generation, "vdir": vdir, "base_version": base_version, "source_key": skey})
                release_lease(pdir, lease, base_version)
                return {"status": "PENDING_CLOUD_ACK", "version": base_version, "notes": notes, "written": len(written)}
            ack = driveid.wait_cloud_ack(reg["root"]["item_id"], root, [pdir / r for r in files], timeout=timeout,
                                         scratch=work())
            if ack["status"] != "ACK":
                _mark_pending(slug, "CONTENT_UNACKED", version, reason,
                              {"generation": generation, "vdir": vdir, "base_version": base_version, "source_key": skey})
                unacked = [k for k, v in ack.get("files", {}).items() if v.get("status") != "ACK"][:5]
                release_lease(pdir, lease, base_version)
                return {"status": "PENDING_CLOUD_ACK", "version": base_version, "notes": notes,
                        "detail": f"{ack['status']} {ack.get('reason') or ''} {unacked}".strip()}
            prefix = f"{e['folder']}/"
            for rel in files:
                files[rel]["drive_id"] = ack["files"][prefix + rel]["drive_id"]
            # 6. compare-and-swap: nobody committed meanwhile, lease still ours, local canonical unchanged
            remote2, remote2_sha = read_manifest(pdir)
            if remote2_sha != remote_sha:
                raise DriveError("STALE_WRITER", f"remote manifest changed during publish (now v{(remote2 or {}).get('version')}); "
                                 "nothing committed", core.EXIT_CONFLICT)
            cur_lease = read_lease(pdir)
            if not cur_lease or cur_lease.get("lease_id") != lease["lease_id"]:
                raise DriveError("LEASE_LOST", "another writer took the lease; nothing committed", core.EXIT_CONFLICT)
            p = core.project_dir(slug)
            man_now = core.project_manifest(slug)
            changed = (int(man_now.get("memory_version", 0)) != src["memory_version"]
                       or man_now.get("current_checkpoint") != src["current_checkpoint"])
            if mode == "canonical":
                changed = changed or core.sha256_file(p / "CURRENT.md") != src["current_sha256"] or \
                    (core.sha256_file(p / "NEXT.md") if (p / "NEXT.md").exists() else sha256_bytes(b"")) != src["next_sha256"]
            if changed:
                _mark_pending(slug, "LOCAL_CHANGED_DURING_PUSH", version, reason)
                raise DriveError("LOCAL_CHANGED_DURING_PUSH", "local CURRENT/NEXT changed; retry", core.EXIT_WARN)
            # 7. commit marker
            manifest = {
                "schema": SCHEMA, "slug": slug, "project_name": man.get("name", slug), "version": version,
                "parent_version": base_version, "parent_manifest_sha256": remote_sha, "generation": generation,
                "status": "COMMITTED", "published_at": iso(), "reason": reason,
                "writer": {**who, "engine_version": core.VERSION, "lease_id": lease["lease_id"]},
                "identity": {"scheme": driveid.SCHEME, "my_drive_item_id": reg["root"]["my_drive_item_id"],
                             "root_item_id": reg["root"]["item_id"], "project_item_id": e["item_id"],
                             "root_name": ROOT_NAME, "project_folder": e["folder"]},
                "source": src, "physical": info["physical"], "reconciliation": info["reconciliation"],
                "current_version_dir": vdir, "files": files, "checkpoints_mirrored": info["checkpoints_mirrored"],
                "content_cloud_ack": {"status": ack["status"], "source": ack["source"], "elapsed_s": ack["elapsed_s"]},
                "read_order": READ_ORDER, "hot_context_rule": HOT_RULE, "proposal_rule": PROPOSAL_RULE,
                "authority_law": LAW, "notes": notes}
            mb = dumps(manifest).encode()
            hits = secret_hits(mb.decode())
            if hits and not allow_secret:
                raise DriveError("REFUSED_SECRET", "MANIFEST.json:" + "+".join(hits), core.EXIT_CONFLICT)
            mount_write(pdir / MANIFEST, mb)
            committed = True
            if sha256_path(pdir / MANIFEST) != sha256_bytes(mb):
                raise DriveError("MOUNT_READBACK_FAILED", MANIFEST)
            mack = driveid.wait_cloud_ack(reg["root"]["item_id"], root, [pdir / MANIFEST], timeout=timeout, scratch=work())
            status = "VERIFIED" if mack["status"] == "ACK" else "COMMITTED_UNACKED"
            st_new = {"slug": slug, "version": version, "generation": generation, "manifest_sha256": sha256_bytes(mb),
                      "manifest_drive_id": (mack["files"].get(prefix + MANIFEST) or {}).get("drive_id"),
                      "status": status, "committed_at": manifest["published_at"],
                      "verified_at": iso() if status == "VERIFIED" else None, "source": src,
                      "current_version_dir": vdir, "project_item_id": e["item_id"],
                      "attachments": (st or {}).get("attachments", []), "last_notes": notes}
            save_state(slug, st_new)
            if status == "VERIFIED":
                pending_path(slug).unlink(missing_ok=True)
            else:
                _mark_pending(slug, "MANIFEST_UNACKED", version, reason)
            _write_root_index(reg, root)
            release_lease(pdir, lease, version)
            return {"status": status, "version": version, "generation": generation, "notes": notes,
                    "written": len(written), "files": len(files), "content_ack_s": ack["elapsed_s"],
                    "manifest_ack_s": mack["elapsed_s"]}
        except BaseException:
            if not committed:
                release_lease(pdir, lease, base_version)
            raise
        finally:
            if stage is not None:
                shutil.rmtree(stage, ignore_errors=True)


def _mark_pending(slug, why, version, reason, extra=None):
    core.atomic_write_json(pending_path(slug), {"slug": slug, "why": why, "attempted_version": version,
                                                "reason": reason, "time": iso(), **(extra or {})})


def _reuse_pending_version(slug, pdir, stage, files, base_version, skey):
    """A previous attempt already uploaded a complete, uncommitted VERSIONS/<dir> for the same parent and the same
    local source: reuse its generation and hot files, so a slow Drive converges instead of re-uploading forever."""
    pend = core.try_load_json(pending_path(slug), {}) or {}
    if not (pend.get("generation") and pend.get("base_version") == base_version and pend.get("source_key") == skey):
        return None
    vdir = pdir / str(pend.get("vdir") or "")
    vj = core.try_load_json(vdir / "VERSION.json", None) if pend.get("vdir") else None
    if not isinstance(vj, dict) or vj.get("generation") != pend["generation"] or vj.get("version") != base_version + 1:
        return None
    for n in HOT:
        ent = (vj.get("files") or {}).get(n) or {}
        if not (vdir / n).is_file() or sha256_path(vdir / n) != ent.get("sha256"):
            return None
    for n in HOT:
        data = (vdir / n).read_bytes()
        (stage / n).write_bytes(data)
        files[n] = {**files[n], "sha256": sha256_bytes(data), "md5": hashlib.md5(data).hexdigest(), "bytes": len(data)}
    return pend["generation"]


def _write_root_index(reg, root):
    """Non-authoritative convenience index + protocol at the Drive root. Best effort."""
    try:
        rows = {}
        for slug, e in sorted(reg["projects"].items()):
            if not e.get("enabled", True):
                continue
            st = load_state(slug) or {}
            rows[slug] = {"folder": e["folder"], "folder_item_id": e["item_id"], "version": st.get("version"),
                          "status": st.get("status"), "committed_at": st.get("committed_at"),
                          "manifest_drive_id": st.get("manifest_drive_id")}
        idx = dumps({"schema": SCHEMA, "authority_law": LAW, "root_item_id": reg["root"]["item_id"],
                     "updated_at": iso(), "projects": rows,
                     "rule": "Navigation only. Per-project MANIFEST.json is the commit marker; verify it, not this index."})
        docs = [("PROJECTS.json", idx), ("README_AGENTS.md", render_protocol())]
        instr = core.ROOT / "docs" / "v4" / "DRIVE_AGENT_INSTRUCTIONS.md"
        if instr.is_file() and not secret_hits(instr.read_text(errors="ignore")):
            docs.append(("AGENT_INSTRUCTIONS.md", instr.read_text(errors="ignore")))
        for name, data in docs:
            f = root / name
            if not f.is_file() or f.read_text(errors="ignore") != data or name == "PROJECTS.json":
                mount_write(f, data.encode())
    except Exception:
        pass


# ------------------------------------------------------------------ read-side commands

def compare(slug, remote):
    """Local vs Drive: MATCH | LOCAL_NEWER | DRIVE_NEWER | DIVERGED | UNPUBLISHED."""
    if not remote:
        return "UNPUBLISHED"
    p = core.project_dir(slug)
    man = core.project_manifest(slug)
    lv = int(man.get("memory_version", 0))
    rs = remote.get("source") or {}
    rv = int(rs.get("memory_version") or 0)
    cur = core.sha256_file(p / "CURRENT.md")
    nxt = core.sha256_file(p / "NEXT.md") if (p / "NEXT.md").exists() else sha256_bytes(b"")
    if rs.get("current_sha256") == cur and rs.get("next_sha256") == nxt and rv == lv:
        return "MATCH"
    if rv == lv and rs.get("current_checkpoint") == man.get("current_checkpoint"):
        # Drive holds exactly the accepted checkpoint; local only has uncheckpointed edits in progress.
        meta = _checkpoint_meta(p, man.get("current_checkpoint")) or {}
        if meta and rs.get("current_sha256") == meta.get("current_sha256") \
                and rs.get("next_sha256") == (meta.get("next_sha256") or sha256_bytes(b"")):
            return "MATCH_ACCEPTED_CHECKPOINT"
    if rv > lv:
        return "DRIVE_NEWER"
    if rv == lv and rs.get("current_checkpoint") != man.get("current_checkpoint"):
        return "DIVERGED"
    return "LOCAL_NEWER"


def verify_remote_files(pdir, remote, names=None):
    """Hash the committed files on the mount. Returns {rel: 'OK'|'MISMATCH'|'MISSING'}."""
    out = {}
    for rel, ent in remote["files"].items():
        if names is not None and rel not in names:
            continue
        f = pdir / rel
        if not f.is_file():
            out[rel] = "MISSING"
        else:
            out[rel] = "OK" if sha256_path(f) == ent.get("sha256") else "MISMATCH"
    return out


def status(slug):
    reg = load_reg()
    res = {"slug": slug, "identity": "UNVERIFIED"}
    root = root_path(reg)
    pdir = project_path(reg, slug, root)
    res["identity"] = "PASS"
    remote, rsha = read_manifest(pdir)
    st = load_state(slug) or {}
    lease = read_lease(pdir)
    res.update({
        "drive_root": str(root), "root_item_id": reg["root"]["item_id"], "project_item_id": project_entry(reg, slug)["item_id"],
        "remote_version": (remote or {}).get("version"), "remote_generation": (remote or {}).get("generation"),
        "remote_memory_version": ((remote or {}).get("source") or {}).get("memory_version"),
        "remote_checkpoint": ((remote or {}).get("source") or {}).get("current_checkpoint"),
        "remote_published_at": (remote or {}).get("published_at"),
        "local_state_version": st.get("version"), "local_state_status": st.get("status"),
        "remote_known_to_local_state": bool(remote and st.get("manifest_sha256") == rsha),
        "local_memory_version": int(core.project_manifest(slug).get("memory_version", 0)),
        "compare": compare(slug, remote),
        "lease": {"state": (lease or {}).get("state"), "live": lease_live(lease),
                  "holder": (lease or {}).get("holder"), "expires_at": (lease or {}).get("expires_at")},
        "inbox": inbox_items(pdir), "pending": core.try_load_json(pending_path(slug), None)})
    return res


def verify(slug, timeout=None):
    """Full check: identity, manifest, mount hashes, server acknowledgement + stable drive ids, local match."""
    reg = load_reg()
    timeout = reg["settings"]["cloud_ack_timeout_s"] if timeout is None else timeout
    r = {"STABLE_DRIVE_IDENTITY": "FAIL", "MANIFEST": "FAIL", "MOUNT_READBACK": "FAIL", "REMOTE_READBACK": "FAIL",
         "DRIVE_IDS_STABLE": "FAIL", "VERSION_DIR_COMPLETE": "FAIL", "SECRET_SCAN": "FAIL", "LOCAL_MATCH": "NO"}
    root = root_path(reg)
    pdir = project_path(reg, slug, root)
    r["STABLE_DRIVE_IDENTITY"] = "PASS"
    with core.lock(f"drive.{slug}", timeout=120):
        remote, rsha = read_manifest(pdir)
        if not remote:
            r["MANIFEST"] = "MISSING"
            return r
        r["MANIFEST"] = "PASS"
        r["VERSION"] = remote["version"]
        mounts = verify_remote_files(pdir, remote)
        r["MOUNT_READBACK"] = "PASS" if all(v == "OK" for v in mounts.values()) else \
            "FAIL " + ",".join(f"{k}={v}" for k, v in mounts.items() if v != "OK")[:400]
        vdir = remote.get("current_version_dir") or ""
        r["VERSION_DIR_COMPLETE"] = "PASS" if vdir and all(mounts.get(f"{vdir}/{n}") == "OK" for n in HOT) else "FAIL"
        hot_hits = []
        for rel in list(HOT) + list(JOURNALS) + [MANIFEST]:
            f = pdir / rel
            if f.is_file():
                h = secret_hits(f.read_text(errors="ignore"))
                if h:
                    hot_hits.append(f"{rel}:{'+'.join(h)}")
        r["SECRET_SCAN"] = "PASS" if not hot_hits else "FAIL " + ";".join(hot_hits)
        prefix = project_entry(reg, slug)["folder"] + "/"
        present = [pdir / rel for rel, v in mounts.items() if v == "OK"] + [pdir / MANIFEST]
        ack = driveid.wait_cloud_ack(reg["root"]["item_id"], root, present, timeout=timeout, scratch=work())
        r["REMOTE_READBACK"] = "PASS" if ack["status"] == "ACK" and r["MOUNT_READBACK"] == "PASS" else \
            f"FAIL {ack['status']} {ack.get('reason') or ''}".strip()
        r["REMOTE_READBACK_SOURCE"] = ack.get("source")
        ids_ok = ack["status"] == "ACK" and all(
            (ack["files"].get(prefix + rel) or {}).get("drive_id") == ent.get("drive_id")
            for rel, ent in remote["files"].items() if ent.get("drive_id"))
        r["DRIVE_IDS_STABLE"] = "PASS" if ids_ok else "FAIL"
        r["MANIFEST_DRIVE_ID"] = (ack["files"].get(prefix + MANIFEST) or {}).get("drive_id")
        cmp = compare(slug, remote)
        r["LOCAL_COMPARE"] = cmp
        r["LOCAL_MATCH"] = "YES" if cmp in ("MATCH", "MATCH_ACCEPTED_CHECKPOINT") else "NO"
        r["SOURCE_MODE"] = (remote.get("source") or {}).get("mode", "canonical")
        st = load_state(slug) or {}
        if st.get("manifest_sha256") == rsha and all(r[k] == "PASS" for k in ("MOUNT_READBACK", "REMOTE_READBACK", "DRIVE_IDS_STABLE")):
            st.update(status="VERIFIED", verified_at=iso(), manifest_drive_id=r["MANIFEST_DRIVE_ID"])
            save_state(slug, st)
            pending_path(slug).unlink(missing_ok=True) if (core.try_load_json(pending_path(slug), {}) or {}).get("why") == "MANIFEST_UNACKED" else None
        r["LOCAL_STATE"] = st.get("status")
    return r


def pull(slug):
    """Copy the committed hot set (verified) + INBOX proposals into <root>/.drive/pulled/<slug>/. Never touches
    canonical local memory."""
    reg = load_reg()
    root = root_path(reg)
    pdir = project_path(reg, slug, root)
    remote, rsha = read_manifest(pdir)
    if not remote:
        raise DriveError("REMOTE_MANIFEST_MISSING", "nothing published yet")
    dest = work() / "pulled" / slug / f"v{remote['version']:06d}"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    used = {}
    vdir = remote.get("current_version_dir")
    for n in HOT:
        ent = remote["files"].get(n) or {}
        top, alt = pdir / n, pdir / (vdir or "") / n
        src = None
        for cand in (top, alt):
            if cand.is_file() and sha256_path(cand) == ent.get("sha256"):
                src = cand
                break
        if src is None:
            raise DriveError("REMOTE_CONTENT_UNVERIFIED", f"{n} matches neither top-level nor {vdir}", core.EXIT_CONFLICT)
        shutil.copyfile(src, dest / n)
        used[n] = "top-level" if src == top else vdir
    (dest / MANIFEST).write_bytes((pdir / MANIFEST).read_bytes())
    inbox = []
    for name in inbox_items(pdir):
        f = pdir / "INBOX" / name
        if f.stat().st_size > 200_000:
            inbox.append({"name": name, "status": "TOO_LARGE_NOT_COPIED"})
            continue
        text = f.read_text(errors="ignore")
        hits = secret_hits(text)
        if hits:
            inbox.append({"name": name, "status": "REFUSED_SECRET:" + "+".join(hits)})
            continue
        (dest / "inbox").mkdir(exist_ok=True)
        (dest / "inbox" / name).write_text(text)
        inbox.append({"name": name, "status": "COPIED_FOR_REVIEW"})
    return {"version": remote["version"], "generation": remote["generation"], "dest": str(dest), "sources": used,
            "compare": compare(slug, remote), "inbox": inbox,
            "rule": "Review copies only. Local canonical memory is unchanged; merge proposals deliberately, then finish."}


def reconcile_cmd(slug, apply=False, agent="unknown", session=None):
    from . import reconcile as reconmod
    reg = load_reg()
    root = root_path(reg)
    pdir = project_path(reg, slug, root)
    remote, rsha = read_manifest(pdir)
    st = load_state(slug) or {}
    live = core.repo_state_for(slug)
    rec = reconmod.reconcile(slug, live)
    cmp = compare(slug, remote)
    dphys = (remote or {}).get("physical") or {}
    if not live.get("is_git_repo"):
        phys = "RUNTIME_VERIFICATION_REQUIRED"
    elif not remote:
        phys = "NO_DRIVE_STATE"
    elif dphys.get("head") == live.get("head") and bool(dphys.get("dirty")) == bool(live.get("dirty")):
        phys = "PHYSICAL_MATCHES_DRIVE"
    else:
        phys = "PHYSICAL_OVERRIDES_DRIVE"
    known = bool(remote and st.get("manifest_sha256") == rsha)
    if remote and not known:
        try:
            classify(remote, rsha, st, {"memory_version": int(core.project_manifest(slug).get("memory_version", 0)),
                                        "current_checkpoint": core.project_manifest(slug).get("current_checkpoint")},
                     reg["writer_root_id"])
            unknown = None
        except DriveError as e:
            unknown = e.code
    else:
        unknown = None
    if unknown:
        action = f"MANUAL_RECONCILIATION ({unknown}: the remote manifest is not the one this root last committed; " \
                 "aimem drive pull, compare with physical state; never auto-merge)"
    elif cmp in ("MATCH", "MATCH_ACCEPTED_CHECKPOINT"):
        action = "NONE" if known and st.get("status") == "VERIFIED" else "VERIFY"
    elif cmp in ("LOCAL_NEWER", "UNPUBLISHED"):
        action = "PUSH"
    else:
        action = "MANUAL_RECONCILIATION (aimem drive pull; compare with physical state; never auto-merge)"
    res = {"slug": slug, "physical_vs_drive": phys, "live_head": live.get("head"), "live_dirty": live.get("dirty"),
           "drive_head": dphys.get("head"), "local_reconciliation": rec["status"], "local_flags": rec["flags"],
           "local_vs_drive": cmp, "remote_known_to_local_state": known, "remote_conflict": unknown, "inbox": inbox_items(pdir),
           "lease": (read_lease(pdir) or {}).get("state"), "recommended_action": action,
           "authority": "fresh physical state > local CURRENT/NEXT > Drive mirror"}
    if apply and not unknown:
        if action == "PUSH":
            res["applied"] = push(slug, agent=agent, session=session, reason="reconcile")
        elif action == "VERIFY":
            res["applied"] = verify(slug)
    return res


def attach(slug, file, kind, note="", agent="unknown", session=None):
    """Cold evidence/artifact upload (explicit only). Recorded in the index on the next push."""
    reg = load_reg()
    src = Path(file).expanduser().resolve()
    if not src.is_file():
        raise DriveError("ATTACH_SOURCE_MISSING", str(src))
    size = src.stat().st_size
    if size > int(reg["settings"]["max_attach_bytes"]):
        raise DriveError("ATTACH_TOO_LARGE", f"{size} bytes; keep it local and index its hash/path")
    if src.suffix.lower() in core.TEXT_EXTS:
        hits = secret_hits(src.read_text(errors="ignore"))
        if hits:
            raise DriveError("REFUSED_SECRET", f"{src.name}:{'+'.join(hits)}", core.EXIT_CONFLICT)
    sha = sha256_path(src)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", src.name)[:80]
    sub = "EVIDENCE_INDEX" if kind == "evidence" else "ARTIFACT_INDEX"
    with core.lock(f"drive.{slug}", timeout=120):
        root = root_path(reg)
        pdir = project_path(reg, slug, root)
        dst = pdir / sub / "cold" / f"{sha[:16]}__{safe}"
        if not (dst.is_file() and sha256_path(dst) == sha):
            mount_write(dst, src.read_bytes())
        ack = driveid.wait_cloud_ack(reg["root"]["item_id"], root, [dst], timeout=reg["settings"]["cloud_ack_timeout_s"],
                                     scratch=work())
        st = load_state(slug) or {"slug": slug}
        row = {"kind": kind, "name": src.name, "sha256": sha, "bytes": size, "local_path": str(src),
               "drive_path": dst.relative_to(pdir).as_posix(), "note": core.redact(note, 500), "attached_at": iso(),
               "agent": agent, "session": session, "cloud_ack": ack["status"],
               "drive_id": (ack["files"].get(dst.relative_to(root).as_posix()) or {}).get("drive_id")}
        st["attachments"] = [a for a in st.get("attachments", []) if a.get("sha256") != sha] + [row]
        save_state(slug, st)
    return row


# ------------------------------------------------------------------ setup

def pin(account=None, accept_new_root=None):
    """Pin (or re-verify) the Drive root by stable identity. A changed root id needs --accept-new-root <id>."""
    with core.lock("drive-root", timeout=60):
        old = load_reg(required=False)
        p = discover_root(account or ((old or {}).get("root") or {}).get("account"))
        ident = driveid.identity(p)
        if not ident["item_id"]:
            ident["item_id"] = driveid.wait_item_id(p, timeout=120)
        if not (ident["account"] and ident["item_id"] and ident["my_drive_item_id"]):
            raise DriveError("DRIVE_IDENTITY_UNVERIFIABLE", "Drive item ids unreadable; is Google Drive for desktop running?")
        if old and any(ident[k] != old["root"].get(k) for k in ("account", "item_id", "my_drive_item_id")):
            if accept_new_root != ident["item_id"]:
                raise DriveError("DRIVE_ROOT_ID_CHANGED", f"pinned {old['root'].get('item_id')} now {ident['item_id']}; "
                                 "re-pin only with --accept-new-root <new id> after checking the folder", core.EXIT_CONFLICT)
        reg = old or {"version": 1, "identity_scheme": driveid.SCHEME, "writer_root_id": uuid.uuid4().hex,
                      "projects": {}, "settings": {}, "created_at": iso()}
        reg["root"] = {**ident, "path": str(p), "name": ROOT_NAME, "pinned_at": iso()}
        save_reg(reg)
        _write_root_index(load_reg(), p)
        return reg["root"]


def register(slug, folder=None):
    core.safe_component(slug, "slug")
    if not core.project_exists(slug):
        raise DriveError("UNKNOWN_PROJECT", slug)
    folder = folder or slug
    core.safe_component(folder, "folder")
    with core.lock("drive-root", timeout=60):
        reg = load_reg()
        root = root_path(reg)
        if any(e.get("folder") == folder for s, e in reg["projects"].items() if s != slug):
            raise DriveError("FOLDER_ALREADY_MAPPED", folder)
        d = root / folder
        if d.is_symlink():
            raise DriveError("SYMLINK_REFUSED", str(d))
        d.mkdir(exist_ok=True)
        for sub in SUBDIRS:
            (d / sub).mkdir(exist_ok=True)
        fid = driveid.wait_item_id(d, timeout=180)
        if not fid:
            raise DriveError("DRIVE_IDENTITY_UNVERIFIABLE", f"{folder} has no server id yet; retry register")
        old = reg["projects"].get(slug)
        if old and old.get("item_id") and old["item_id"] != fid:
            raise DriveError("PROJECT_FOLDER_ID_CHANGED", f"pinned {old['item_id']} now {fid}", core.EXIT_CONFLICT)
        reg["projects"][slug] = {"folder": folder, "item_id": fid, "enabled": True,
                                 "registered_at": (old or {}).get("registered_at") or iso()}
        save_reg(reg)
        return reg["projects"][slug]


# ------------------------------------------------------------------ engine hooks (never fatal)

def enabled_for(slug):
    try:
        reg = load_reg(required=False)
        return bool(reg and (reg["projects"].get(slug) or {}).get("enabled", False)), reg
    except (Exception, SystemExit):
        return False, None


def _with_timeout(fn, seconds):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001 - reported to the caller
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise DriveError("TIMEOUT", f"{seconds}s")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def begin_lines(slug, session_id=None):
    """Bounded, read-only Drive reconciliation for `aimem begin`: MANIFEST.json only, no cloud wait."""
    ok, reg = enabled_for(slug)
    if not ok or not reg["settings"].get("begin_check", True):
        return []
    try:
        s = _with_timeout(lambda: status(slug), 15)
    except (Exception, SystemExit) as e:
        code = e.code if isinstance(e, DriveError) else type(e).__name__
        return [f"DRIVE_STATE=UNAVAILABLE REASON={code}"]
    line = (f"DRIVE_STATE={s['compare']} DRIVE_VERSION={s['remote_version']} "
            f"DRIVE_MEMORY_VERSION={s['remote_memory_version']} IDENTITY={s['identity']}")
    out = [line]
    if s["lease"]["live"]:
        h = s["lease"]["holder"] or {}
        out.append(f"DRIVE_LEASE=HELD agent={h.get('agent')} session={h.get('session')} expires={s['lease']['expires_at']}")
    if s["inbox"]:
        out.append(f"DRIVE_INBOX={len(s['inbox'])} (review: aimem drive pull {slug})")
    if s["compare"] in ("DRIVE_NEWER", "DIVERGED") or (s["remote_version"] and not s["remote_known_to_local_state"]):
        out.append(f"DRIVE_RECONCILE_REQUIRED=aimem drive reconcile {slug}")
    if s["pending"]:
        out.append(f"DRIVE_PENDING={s['pending'].get('why')} (retry: aimem drive push {slug})")
    return out


def after_checkpoint(slug, session=None, agent=None):
    """Post-commit publish for finish/checkpoint. Local success is never converted into failure."""
    ok, reg = enabled_for(slug)
    if not ok or not reg["settings"].get("finish_publish", True):
        return
    try:
        if agent is None and session:
            m = re.match(r"^\d{8}T\d{6}[+-]\d{4}-([A-Za-z0-9_]+)-", session)
            agent = m.group(1) if m else None
        r = push(slug, agent=agent or "aimem", session=session, timeout=reg["settings"]["finish_ack_timeout_s"],
                 reason="finish", out=lambda *_: None)
        extra = " ".join(r.get("notes") or [])
        print(f"DRIVE_SYNC={r['status']} DRIVE_VERSION={r.get('version')}" + (f" NOTES={extra}" if extra else "")
              + ("" if r["status"] in ("VERIFIED", "UP_TO_DATE") else f" RETRY=aimem_drive_verify_{slug}"))
    except DriveError as e:
        try:
            if e.code not in ("LEASE_HELD",):
                _mark_pending(slug, e.code, None, "finish")
        except Exception:
            pass
        print(f"DRIVE_SYNC={'CONFLICT' if e.exit_code == core.EXIT_CONFLICT else 'FAILED'} REASON={e.code}"
              + (f" DETAIL={e.detail[:200]}" if e.detail else "") + f" RETRY=aimem_drive_reconcile_{slug}")
    except (Exception, SystemExit) as e:
        try:
            _mark_pending(slug, type(e).__name__, None, "finish")
        except Exception:
            pass
        print(f"DRIVE_SYNC=FAILED REASON={type(e).__name__} RETRY=aimem_drive_push_{slug}")


def sweep_retry():
    """Retry pending publications (called by aimem-sweep). Conflicts stay pending for a human/agent decision."""
    reg = load_reg(required=False)
    if not reg:
        return []
    out = []
    for f in sorted((work() / "pending").glob("*.json")) if (work() / "pending").is_dir() else []:
        j = core.try_load_json(f, {}) or {}
        slug = j.get("slug")
        if not slug or not (reg["projects"].get(slug) or {}).get("enabled"):
            continue
        if j.get("why") in ("REFUSED_SECRET", "REMOTE_NEWER", "DIVERGED", "CONFLICT_FOREIGN_WRITER", "REMOTE_REWRITTEN",
                            "REMOTE_ROLLED_BACK", "REMOTE_MANIFEST_MISSING", "STALE_WRITER", "LEASE_MALFORMED"):
            out.append(f"{slug}:SKIPPED_{j.get('why')}")
            continue
        try:
            if j.get("why") == "MANIFEST_UNACKED":
                r = verify(slug, timeout=reg["settings"]["sweep_ack_timeout_s"])
                out.append(f"{slug}:VERIFY_{r.get('REMOTE_READBACK', '?').split()[0]}")
            else:
                r = push(slug, agent="aimem-sweep", timeout=reg["settings"]["sweep_ack_timeout_s"], reason="sweep-retry",
                         out=lambda *_: None)
                out.append(f"{slug}:{r['status']}")
        except (Exception, SystemExit) as e:
            out.append(f"{slug}:{getattr(e, 'code', type(e).__name__)}")
    return out


# ------------------------------------------------------------------ CLI

def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def cli(a):
    try:
        act = a.drive_action
        if act == "pin":
            r = pin(a.account, a.accept_new_root)
            print(f"DRIVE_ROOT_PINNED scheme={r['identity_scheme']} item_id={r['item_id']} my_drive={r['my_drive_item_id']}")
            return
        if act == "register":
            r = register(a.slug, a.folder)
            print(f"DRIVE_PROJECT_REGISTERED slug={a.slug} folder={r['folder']} item_id={r['item_id']}")
            return
        if act == "status":
            _print(status(a.slug))
            return
        if act == "push":
            r = push(a.slug, agent=a.agent, session=a.session, timeout=a.timeout, wait=not a.no_wait,
                     allow_secret=a.allow_secret_pattern, backfill=a.backfill_checkpoints, reason="manual", mode=a.source)
            _print(r)
            print(f"DRIVE_SYNC={r['status']} DRIVE_VERSION={r.get('version')}")
            if r["status"] not in ("VERIFIED", "UP_TO_DATE"):
                raise SystemExit(core.EXIT_WARN)
            return
        if act == "pull":
            _print(pull(a.slug))
            return
        if act == "verify":
            r = verify(a.slug, a.timeout)
            for k in sorted(r):
                print(f"{k}={r[k]}")
            if any(str(r.get(k, "")).split(" ")[0] != "PASS" for k in
                   ("STABLE_DRIVE_IDENTITY", "MANIFEST", "MOUNT_READBACK", "REMOTE_READBACK", "DRIVE_IDS_STABLE")):
                raise SystemExit(core.EXIT_WARN)
            return
        if act == "reconcile":
            _print(reconcile_cmd(a.slug, a.apply, a.agent, a.session))
            return
        if act == "attach":
            _print(attach(a.slug, a.file, a.kind, a.note or "", a.agent, a.session))
            return
        if act == "lease":
            reg = load_reg()
            pdir = project_path(reg, a.slug)
            cur = read_lease(pdir)
            if a.release_expired:
                if cur and cur.get("state") == "HELD" and not lease_live(cur):
                    release_lease(pdir, cur)
                    print("DRIVE_LEASE=RELEASED_EXPIRED")
                else:
                    print("DRIVE_LEASE=NOT_EXPIRED_OR_NOT_HELD (live leases are never broken)")
                    raise SystemExit(core.EXIT_CONFLICT)
                return
            _print(cur)
            return
    except DriveError as e:
        print(f"DRIVE_ERROR={e.code}" + (f" DETAIL={e.detail}" if e.detail else ""))
        raise SystemExit(e.exit_code)


def add_parser(sp):
    p = sp.add_parser("drive", help="Verified Google Drive mirror/coordination layer for local AI Memory")
    d = p.add_subparsers(dest="drive_action", required=True)
    q = d.add_parser("pin", help="pin/re-verify My Drive/AI-Memory by stable Drive ids")
    q.add_argument("--account"); q.add_argument("--accept-new-root", metavar="ITEM_ID")
    q = d.add_parser("register", help="create/pin <root>/<folder> for a registered project")
    q.add_argument("slug"); q.add_argument("--folder")
    for name in ("status", "pull"):
        q = d.add_parser(name); q.add_argument("slug")
    q = d.add_parser("push", help="atomic publish with remote readback")
    q.add_argument("slug"); q.add_argument("--agent", default="unknown"); q.add_argument("--session")
    q.add_argument("--timeout", type=float); q.add_argument("--no-wait", action="store_true")
    q.add_argument("--allow-secret-pattern", action="store_true", help="verified false positive only")
    q.add_argument("--backfill-checkpoints", type=int, default=0, metavar="N", help="also mirror the N most recent checkpoints")
    q.add_argument("--source", choices=["auto", "canonical", "checkpoint"], default="auto",
                   help="auto: live CURRENT/NEXT unless another active session has uncheckpointed edits")
    q = d.add_parser("verify"); q.add_argument("slug"); q.add_argument("--timeout", type=float)
    q = d.add_parser("reconcile"); q.add_argument("slug"); q.add_argument("--apply", action="store_true")
    q.add_argument("--agent", default="unknown"); q.add_argument("--session")
    q = d.add_parser("attach", help="explicit cold evidence/artifact upload")
    q.add_argument("slug"); q.add_argument("--file", required=True); q.add_argument("--kind", choices=["evidence", "artifact"], required=True)
    q.add_argument("--note"); q.add_argument("--agent", default="unknown"); q.add_argument("--session")
    q = d.add_parser("lease"); q.add_argument("slug"); q.add_argument("--release-expired", action="store_true")
    p.set_defaults(func=cli)
