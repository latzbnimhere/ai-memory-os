"""AI Memory OS V4 core: paths, config, atomic I/O, locks, git helpers, secrets.

Local-only. Python standard library only. No network.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VERSION = "4.1.0"
ENGINE_LINEAGE = ["2.0.0 (V2)", "2.0.0+V3.1 (session binding)", "2.0.0+V3.1.1 (known-good baseline)", "4.0.0 (V4)", "4.0.1 (backup sidecar naming fix)", "4.1.0 (adaptive context + fail-closed activity heartbeat)"]
PATCH_LEVEL = "V4.1.0"

ROOT = Path(os.environ.get("AI_MEMORY_ROOT", str(Path.home() / "AI-Memory"))).expanduser()
PROJECTS = ROOT / "projects"
REGISTRY = ROOT / "registry" / "projects.json"
CONFIG = ROOT / "config.json"
DB = ROOT / "registry" / "memory.db"
LOCKS = ROOT / ".locks"
OBJECTS = ROOT / "objects"
RUN = ROOT / ".run"
GENERATED_IGNORE = {".generated", ".git", "backups", ".txn", ".locks", ".run", "objects"}

# Exit codes (documented in docs/COMMANDS.md)
EXIT_OK = 0
EXIT_WARN = 1
EXIT_ERROR = 2
EXIT_CONFLICT = 3
EXIT_RECOVERY_REQUIRED = 4
EXIT_LOCK_TIMEOUT = 5
EXIT_AMBIGUOUS = 6

DEFAULT_CONFIG = {
    "version": 4,
    "default_context_tokens": 4500,
    "context_clean_target_tokens": 3600,
    "max_current_chars_warning": 24000,
    "max_index_file_bytes": 5_000_000,
    "search_results": 12,
    "checkpoint_recent_events": 60,
    "secret_scan": True,
    "lock_timeout_s": 20,
    "heartbeat_stale_s": 7200,
    "heartbeat_abandoned_s": 86400,
    "sweep_refresh_unique_active_session_on_physical_change": True,
    "dashboard_port": 47119,
    "backup_dir": str(Path.home() / "AI-Memory-Backups"),
    "backup_max_age_days": 7,
    "context_size_warning_tokens": 9000,
}

TEXT_EXTS = {".md", ".txt", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".csv"}

SECRET_PATTERNS = [
    ("OpenAI-like key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic-like key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Slack token", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("Bearer header", re.compile(r"(?i)\bauthorization:\s*bearer\s+[A-Za-z0-9._-]{16,}")),
    ("Cookie header", re.compile(r"(?i)\b(?:set-)?cookie:\s*\S{16,}")),
    # values starting with / ~ . are paths (e.g. "pwd=/repo"), not credentials
    ("Password assignment", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?(?![/~.])[^\s'\"]{6,}")),
]
REDACT_RX = [
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|cookie)\s*[:=]\s*\S+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY(?: BLOCK)?-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
]

# High-confidence credential shapes only: safe to apply to whole generated documents
# (context packs) without mangling ordinary prose such as "token budget: 3600".
REDACT_HIGH_CONFIDENCE_RX = [rx for _label, rx in SECRET_PATTERNS if _label != "Password assignment"]

# ---------------------------------------------------------------- time


def now():
    return dt.datetime.now().astimezone()


def iso():
    return now().isoformat(timespec="seconds")


def stamp():
    return now().strftime("%Y%m%dT%H%M%S%z")


def day():
    return now().strftime("%Y-%m-%d")


def parse_iso(s):
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.astimezone()
        return d
    except Exception:
        return None


# ---------------------------------------------------------------- errors


class AimemError(SystemExit):
    def __init__(self, msg, code=EXIT_ERROR):
        print("ERROR:", msg, file=sys.stderr)
        super().__init__(code)


def die(msg, code=EXIT_ERROR):
    raise AimemError(msg, code)


# ---------------------------------------------------------------- path safety


def safe_component(value, what="name"):
    """Validate a single path component taken from user/agent input (slug, session id).

    Rejects anything that could escape its parent directory or hide from listings:
    empty, '.'/'..', leading dot, path separators, NUL/control characters, over-long.
    Returns the value unchanged when safe; fails closed (EXIT_ERROR) otherwise.
    """
    v = "" if value is None else str(value)
    if (not v or v in (".", "..") or v.startswith(".") or "/" in v or "\\" in v or len(v) > 200
            or any(ord(c) < 32 or ord(c) == 127 for c in v)):
        die(f"UNSAFE_{what.upper()}: {v!r} is not a valid {what} (no path separators, no leading dot, no control characters)")
    return v


# ---------------------------------------------------------------- json / io


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        die(f"Invalid JSON: {path}: {e}")


def try_load_json(path: Path, default=None):
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text())
    except Exception:
        return default


def dumps(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def _fsync_dir(path: Path):
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception:
        pass


def atomic_write(path: Path, text: str, validate=None, mode=None):
    """write temp -> fsync -> validate -> atomic replace -> fsync dir."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if validate is not None:
            validate(Path(tmp))
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def validate_json_file(p: Path):
    json.loads(Path(p).read_text())


def atomic_write_json(path: Path, obj):
    atomic_write(path, dumps(obj), validate=validate_json_file)


def append_jsonl(path: Path, obj):
    """Append one JSON line; exclusive lock on the file, fsync.

    Crash safety: if a previous writer died mid-line (file does not end in a newline),
    a newline is written first so the torn fragment stays an isolated, detectable bad
    line (see `aimem doctor`) instead of silently corrupting this record as well.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def read_jsonl(path: Path, limit=None):
    out = []
    if not Path(path).exists():
        return out
    with Path(path).open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    if limit:
        return out[-limit:]
    return out


def tail_lines(path: Path, n, block=65536):
    """Last n lines of a file without reading the whole file (journals can be huge)."""
    p = Path(path)
    if n <= 0 or not p.exists():
        return []
    with p.open("rb") as f:
        f.seek(0, os.SEEK_END)
        end = f.tell()
        buf = b""
        pos = end
        while pos > 0 and buf.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf
    lines = buf.decode("utf-8", errors="ignore").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]  # first line may be partial
    return lines[-n:]


def read_tail(path: Path, n):
    return "\n".join(tail_lines(path, n))


def tail_jsonl(path: Path, n):
    """Last n parseable JSON records (malformed lines skipped)."""
    out = []
    for line in tail_lines(path, n):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def sha256_file(path: Path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def approx_tokens(text):
    return max(1, int(len(text) / 3.2))


def truncate_to_tokens(text, tokens):
    chars = max(0, int(tokens * 3.2))
    if len(text) <= chars:
        return text
    return text[:chars] + "\n[TRUNCATED_BY_LOCAL_CONTEXT_BUDGET]\n"


# ---------------------------------------------------------------- config / root


def config():
    c = DEFAULT_CONFIG.copy()
    c.update(try_load_json(CONFIG, {}) or {})
    return c


def ensure_root():
    if not ROOT.exists():
        die(f"Memory root does not exist: {ROOT}")
    for d in (PROJECTS, ROOT / "registry", LOCKS, RUN):
        d.mkdir(exist_ok=True)


def version_tuple(v):
    """'4.1.0' -> (4, 1, 0); unparseable -> None (callers must treat None as unknown, never as older)."""
    import re as _re
    m = _re.match(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(v or ""))
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def installed_version():
    v = ROOT / "VERSION"
    if v.exists():
        return v.read_text().strip()
    return "unknown"


# ---------------------------------------------------------------- locks


class LockTimeout(AimemError):
    def __init__(self, name, timeout, holder=""):
        extra = f" last_holder={holder}" if holder else ""
        super().__init__(f"LOCK_TIMEOUT: could not acquire lock '{name}' within {timeout}s (another agent holds it); failing closed.{extra}", EXIT_LOCK_TIMEOUT)


@contextlib.contextmanager
def lock(name="global", timeout=None, required=True):
    """Exclusive advisory lock with bounded wait; fails closed on timeout.

    flock locks are released by the kernel when the holding process dies, so a crashed
    agent never leaves a stale lock behind; the lock file itself is only a rendezvous
    point (it records the last holder for diagnostics). Locks are NOT re-entrant.

    Yields True when acquired. With required=False a timeout yields False (caller proceeds
    without the lock, e.g. searching a slightly stale derived index) instead of failing.
    """
    ensure_root()
    if timeout is None:
        timeout = config().get("lock_timeout_s", 20)
    p = LOCKS / f"{name}.lock"
    f = p.open("a+")
    deadline = time.time() + float(timeout)
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                if time.time() >= deadline:
                    if not required:
                        break
                    try:
                        f.seek(0)
                        holder = f.read(300).strip()
                    except Exception:
                        holder = ""
                    raise LockTimeout(name, timeout, holder)
                time.sleep(0.05)
        if not acquired:
            yield False
            return
        try:
            f.seek(0)
            f.truncate()
            f.write(json.dumps({"pid": os.getpid(), "since": iso(), "argv": " ".join(sys.argv[1:4])[:120]}) + "\n")
            f.flush()
        except Exception:
            pass
        yield True
    finally:
        if acquired:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        f.close()


def project_write_lock(slug, timeout=None):
    return lock(f"{slug}.write", timeout)


def project_step_lock(slug, timeout=None):
    return lock(f"{slug}.step", timeout)


def project_session_lock(slug, timeout=None):
    """Serializes read-modify-write of session state files (heartbeat, finish, close).

    Lock order: session lock -> write lock. Never acquire the session lock while
    holding the write lock.
    """
    return lock(f"{slug}.session", timeout)


# ---------------------------------------------------------------- registry / projects


def registry():
    ensure_root()
    d = load_json(REGISTRY, {"version": 1, "projects": {}}) or {}
    d.setdefault("projects", {})
    return d


def save_registry(d):
    atomic_write(REGISTRY, json.dumps(d, indent=2, sort_keys=True) + "\n", validate=validate_json_file)


def project_dir(slug):
    return PROJECTS / safe_component(slug, "slug")


def project_exists(slug):
    return (project_dir(slug) / "project.json").exists()


def project_manifest(slug):
    p = project_dir(slug) / "project.json"
    if not p.exists():
        die(f"Unknown project: {slug}")
    return load_json(p, {}) or {}


def all_slugs():
    return sorted(registry()["projects"])


def detect_candidates(cwd):
    """All registered projects whose repo contains cwd, most specific (longest repo path) first."""
    reg = try_load_json(REGISTRY, {"projects": {}}) or {"projects": {}}
    target = Path(cwd).expanduser()
    try:
        target = target.resolve()
    except Exception:
        pass
    matches = []
    for slug, rec in reg.get("projects", {}).items():
        repo = (rec.get("repo") or "").strip()
        if not repo:
            continue
        rp = Path(repo).expanduser()
        try:
            rp = rp.resolve()
        except Exception:
            pass
        try:
            target.relative_to(rp)
            matches.append((len(str(rp)), slug))
        except ValueError:
            pass
    matches.sort(key=lambda m: (-m[0], m[1]))
    return matches


def detect_project(cwd):
    """Most specific registered project for cwd; None when unregistered OR ambiguous.

    Two slugs registered for the same repo path is ambiguous: fail closed rather than
    silently binding an agent to whichever slug sorts last.
    """
    matches = detect_candidates(cwd)
    if not matches:
        return None
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        return None
    return matches[0][1]


def recursive_index_risk(repo_path):
    """True when a repo path would make the memory root index itself."""
    if not repo_path:
        return False
    try:
        rp = Path(repo_path).expanduser().resolve()
        rr = ROOT.resolve()
    except Exception:
        return False
    return rp == rr or str(rr).startswith(str(rp) + os.sep) or str(rp).startswith(str(rr) + os.sep)


# ---------------------------------------------------------------- git


def git_cmd(repo: Path, args, timeout=10):
    try:
        r = subprocess.run(["git", "-C", str(repo)] + list(args), text=True, capture_output=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return 127, "", str(e)


def repo_state_for_path(repo_s):
    repo_s = (repo_s or "").strip()
    if not repo_s:
        return {"configured": False, "captured_at": iso()}
    repo = Path(repo_s).expanduser()
    state = {"configured": True, "path": str(repo), "exists": repo.exists(), "captured_at": iso()}
    if not repo.exists():
        return state
    rc, inside, _ = git_cmd(repo, ["rev-parse", "--is-inside-work-tree"])
    state["is_git_repo"] = (rc == 0 and inside == "true")
    if not state["is_git_repo"]:
        return state
    for key, cmd in [("head", ["rev-parse", "HEAD"]), ("branch", ["branch", "--show-current"]), ("tree", ["rev-parse", "HEAD^{tree}"])]:
        rc, out, _ = git_cmd(repo, cmd)
        state[key] = out if rc == 0 else None
    rc, out, _ = git_cmd(repo, ["status", "--porcelain=v1"])
    state["dirty"] = bool(out) if rc == 0 else None
    state["status_lines"] = out.splitlines()[:100] if rc == 0 else []
    return state


def repo_state_for(slug):
    man = project_manifest(slug)
    return repo_state_for_path(man.get("repo") or "")


def git_memory_commit(message):
    if not (ROOT / ".git").exists():
        return
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", "."], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m", message], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except Exception:
        pass


# ---------------------------------------------------------------- secrets


def secret_hits_text(text):
    hits = []
    for label, rx in SECRET_PATTERNS:
        if rx.search(text or ""):
            hits.append(label)
    return hits


def secret_hits(path: Path):
    try:
        return secret_hits_text(Path(path).read_text(errors="ignore"))
    except Exception:
        return []


def redact_high_confidence(text):
    """Redact only unmistakable credential shapes (keys, tokens, private-key headers)."""
    out = text or ""
    for rx in REDACT_HIGH_CONFIDENCE_RX:
        out = rx.sub("[REDACTED_SECRET]", out)
    return out


def redact(value, limit=4000):
    if value is None:
        return None
    out = str(value)
    for rx in REDACT_RX:
        out = rx.sub("[REDACTED_SECRET]", out)
    if len(out) > limit:
        out = out[:limit] + " [TRUNCATED]"
    return out


# ---------------------------------------------------------------- master index


def rebuild_master_index():
    reg = registry()
    lines = ["# MASTER PROJECT INDEX", "", f"LAST_UPDATED: {iso()}", f"AI_MEMORY_OS_VERSION: {VERSION}", ""]
    for slug in sorted(reg["projects"]):
        rec = reg["projects"][slug]
        man = try_load_json(project_dir(slug) / "project.json", {}) or {}
        lines += [
            f"## {rec.get('name', slug)}",
            f"- Slug: `{slug}`",
            f"- Repo: `{rec.get('repo', '')}`" if rec.get("repo") else "- Repo: (not set)",
            f"- Current checkpoint: `{man.get('current_checkpoint')}`" if man.get("current_checkpoint") else "- Current checkpoint: none",
            f"- Memory version: {man.get('memory_version', 0)}",
            f"- Current: `projects/{slug}/CURRENT.md`",
            f"- Next: `projects/{slug}/NEXT.md`",
            "",
        ]
    atomic_write(ROOT / "MASTER_INDEX.md", "\n".join(lines) + "\n")


def human_bytes(n):
    n = float(n or 0)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024


def dir_size(path: Path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total
