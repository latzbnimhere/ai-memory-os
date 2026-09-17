"""Backup / list / isolated restore verification / explicit restore."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from . import core
from .core import ROOT, config, iso, sha256_file, stamp

EXCLUDE_TOP = {".locks", ".run"}
EXCLUDE_DB = {"memory.db", "memory.db-wal", "memory.db-shm"}


def sidecar(tar_path, suffix):
    """Sidecar path for a backup tarball: <name>.tar.gz.<suffix>. Reads fall back to the pre-4.0.1 legacy
    name (<name>.tar.tar.gz.<suffix>, produced by a with_suffix() misuse) so existing backups stay readable."""
    tar_path = Path(tar_path)
    new = Path(str(tar_path) + "." + suffix)
    if new.exists():
        return new
    legacy = tar_path.with_suffix(".tar.gz." + suffix)  # legacy read-only fallback
    return legacy if legacy.exists() else new


def backup_dir():
    d = Path(config().get("backup_dir", str(Path.home() / "AI-Memory-Backups"))).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _manifest(root: Path):
    lines = []
    for r, dirs, files in os.walk(root):
        rel_root = Path(r).relative_to(root)
        parts = rel_root.parts
        if parts and parts[0] in EXCLUDE_TOP:
            dirs[:] = []
            continue
        if ".generated" in parts:
            continue
        for fn in sorted(files):
            if parts and parts[0] == "registry" and fn in EXCLUDE_DB:
                continue
            if fn.endswith(".staged") or fn.endswith(".tmp"):
                continue
            fp = Path(r) / fn
            try:
                lines.append(f"{sha256_file(fp)}  {fp.relative_to(root)}")
            except OSError:
                pass
    return "\n".join(lines) + "\n"


def create(output_dir=None, label=""):
    core.ensure_root()
    outdir = Path(output_dir).expanduser() if output_dir else backup_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    safe = ("-" + "".join(c if c.isalnum() or c in "._-" else "-" for c in label)) if label else ""
    name = f"AI-Memory-{stamp()}{safe}"
    target = outdir / f"{name}.tar.gz"
    manifest = _manifest(ROOT)
    tmp = Path(str(target) + ".partial")

    def flt(ti):
        parts = Path(ti.name).parts
        if len(parts) >= 2 and parts[1] in EXCLUDE_TOP:
            return None
        if ".generated" in parts:
            return None
        if len(parts) >= 3 and parts[1] == "registry" and parts[2] in EXCLUDE_DB:
            return None
        if ti.name.endswith(".staged") or ti.name.endswith(".tmp"):
            return None
        return ti

    with tarfile.open(tmp, "w:gz") as tf:
        for item in sorted(ROOT.iterdir()):
            if item.name in EXCLUDE_TOP:
                continue
            tf.add(item, arcname=f"AI-Memory/{item.name}", filter=flt)
        mtmp = tempfile.NamedTemporaryFile("w", delete=False, suffix=".sha256")
        mtmp.write(manifest)
        mtmp.close()
        tf.add(mtmp.name, arcname="AI-Memory/BACKUP_MANIFEST.sha256")
        os.unlink(mtmp.name)
    with tmp.open("rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, target)
    core.atomic_write(sidecar(target, "sha256"), f"{sha256_file(target)}  {target.name}\n")
    core.atomic_write(sidecar(target, "meta.json"), core.dumps({
        "created": iso(), "engine_version": core.VERSION, "root": str(ROOT), "label": label,
        "bytes": target.stat().st_size, "sha256": sha256_file(target), "manifest_entries": manifest.count("\n")}))
    return target


def list_backups():
    out = []
    for t in sorted(backup_dir().glob("*.tar.gz")):
        meta = core.try_load_json(sidecar(t, "meta.json"), {}) or {}
        ver = core.try_load_json(sidecar(t, "verify.json"), {}) or {}
        age_h = (time.time() - t.stat().st_mtime) / 3600.0
        out.append({"path": str(t), "name": t.name, "bytes": t.stat().st_size, "age_hours": round(age_h, 1),
                    "engine_version": meta.get("engine_version"), "label": meta.get("label"),
                    "verified": ver.get("result"), "verified_at": ver.get("time")})
    return out


def newest():
    b = list_backups()
    return min(b, key=lambda r: r["age_hours"]) if b else None  # newest by mtime, not by name


def _bin_aimem():
    here = Path(__file__).resolve().parents[2] / "bin" / "aimem"
    return here if here.exists() else Path(sys.argv[0]).resolve()


def verify(backup_path, keep_temp=False):
    """backup -> extract to temp -> verify hashes -> doctor on restored copy -> PASS/FAIL -> delete temp."""
    bp = Path(backup_path).expanduser()
    if not bp.exists():
        core.die(f"Backup not found: {bp}")
    report = {"backup": str(bp), "time": iso(), "checks": [], "result": "FAIL"}
    side = sidecar(bp, "sha256")
    if side.exists():
        expected = side.read_text().split()[0]
        actual = sha256_file(bp)
        report["checks"].append(("tarball_sha256", "PASS" if expected == actual else "FAIL"))
        if expected != actual:
            return _finish_verify(bp, report)
    else:
        report["checks"].append(("tarball_sha256", "SKIP_NO_SIDECAR"))
    tmp = Path(tempfile.mkdtemp(prefix="aimem-restore-verify-"))
    try:
        with tarfile.open(bp, "r:gz") as tf:
            for m in tf.getmembers():
                if m.name.startswith("/") or ".." in Path(m.name).parts:
                    report["checks"].append(("tar_paths_safe", "FAIL"))
                    return _finish_verify(bp, report)
            tf.extractall(tmp)
        report["checks"].append(("tar_paths_safe", "PASS"))
        root = tmp / "AI-Memory"
        man = root / "BACKUP_MANIFEST.sha256"
        if man.exists():
            bad = 0
            total = 0
            for line in man.read_text().splitlines():
                if not line.strip():
                    continue
                h, rel = line.split("  ", 1)
                total += 1
                fp = root / rel
                if not fp.exists() or sha256_file(fp) != h:
                    bad += 1
            report["checks"].append(("manifest_hashes", f"PASS ({total} files)" if bad == 0 else f"FAIL ({bad}/{total} mismatched)"))
            if bad:
                return _finish_verify(bp, report)
        else:
            # V3-era backup without manifest: structural check only
            report["checks"].append(("manifest_hashes", "SKIP_NO_MANIFEST (pre-V4 backup)"))
        for req in ("registry/projects.json", "config.json"):
            report["checks"].append((f"exists:{req}", "PASS" if (root / req).exists() else "FAIL"))
        env = dict(os.environ)
        env["AI_MEMORY_ROOT"] = str(root)
        r = subprocess.run([sys.executable, str(_bin_aimem()), "doctor", "--deep", "--no-repo"], env=env, capture_output=True, text=True, timeout=300)
        doc = "PASS" if r.returncode == 0 else f"FAIL rc={r.returncode}"
        report["checks"].append(("doctor_on_restored_copy", doc))
        report["doctor_output"] = (r.stdout + r.stderr)[-3000:]
        if r.returncode != 0:
            return _finish_verify(bp, report)
        report["result"] = "PASS"
        return _finish_verify(bp, report)
    finally:
        if keep_temp:
            report["temp_kept"] = str(tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def _finish_verify(bp, report):
    if any(v.startswith("FAIL") for _k, v in report["checks"]):
        report["result"] = "FAIL"
    try:
        core.atomic_write(sidecar(bp, "verify.json"), core.dumps(report))
    except Exception:
        pass
    return report


def restore(backup_path, confirm=False):
    """Explicit restore: verify first, then move live root aside and extract. Never silent."""
    bp = Path(backup_path).expanduser()
    rep = verify(bp)
    if rep["result"] != "PASS":
        core.die("Restore refused: backup verification FAILED. See verify report.")
    if not confirm:
        core.die("Restore refused: pass --confirm to replace the live memory root (the current root is moved aside, not deleted).", core.EXIT_WARN)
    aside = ROOT.parent / f"{ROOT.name}.pre-restore-{stamp()}"
    tmp = Path(tempfile.mkdtemp(prefix="aimem-restore-", dir=str(ROOT.parent)))
    with tarfile.open(bp, "r:gz") as tf:
        tf.extractall(tmp)
    extracted = tmp / "AI-Memory"
    os.rename(ROOT, aside)
    os.rename(extracted, ROOT)
    shutil.rmtree(tmp, ignore_errors=True)
    return aside
