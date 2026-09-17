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
    ("Password assignment", re.compile(r"(?i)\b(password|passwd|pwd)\s*[:=]\s*['\"]?[^\s'\"]{6,}")),
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
    """Append one JSON line; exclusive lock on the file, fsync."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


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


def read_tail(path: Path, n):
    p = Path(path)
    if not p.exists():
        return ""
    lines = p.read_text(errors="ignore").splitlines()
    return "\n".join(lines[-n:])


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


def installed_version():
    v = ROOT / "VERSION"
    if v.exists():
        return v.read_text().strip()
    return "unknown"


# ---------------------------------------------------------------- locks


class LockTimeout(AimemError):
    def __init__(self, name, timeout):
        super().__init__(f"LOCK_TIMEOUT: could not acquire lock '{name}' within {timeout}s (another agent holds it); failing closed.", EXIT_LOCK_TIMEOUT)


@contextlib.contextmanager
def lock(name="global", timeout=None):
    """Exclusive advisory lock with bounded wait; fails closed on timeout."""
    ensure_root()
    if timeout is None:
        timeout = config().get("lock_timeout_s", 20)
    p = LOCKS / f"{name}.lock"
    f = p.open("w")
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
                    raise LockTimeout(name, timeout)
                time.sleep(0.05)
        yield
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


# ---------------------------------------------------------------- registry / projects


def registry():
    ensure_root()
    d = load_json(REGISTRY, {"version": 1, "projects": {}}) or {}
    d.setdefault("projects", {})
    return d


def save_registry(d):
    atomic_write(REGISTRY, json.dumps(d, indent=2, sort_keys=True) + "\n", validate=validate_json_file)


def project_dir(slug):
    return PROJECTS / slug


def project_exists(slug):
    return (project_dir(slug) / "project.json").exists()


def project_manifest(slug):
    p = project_dir(slug) / "project.json"
    if not p.exists():
        die(f"Unknown project: {slug}")
    return load_json(p, {}) or {}


def all_slugs():
    return sorted(registry()["projects"])


def detect_project(cwd):
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
    if not matches:
        return None
    matches.sort(reverse=True)
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
