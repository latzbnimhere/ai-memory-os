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


class UnsafeArchive(Exception):
    """The tarball contains something that must never be extracted from a memory backup."""


def _contained_symlink(name, linkname):
    import posixpath
    target = posixpath.normpath(posixpath.join(posixpath.dirname(name), linkname))
    return not linkname.startswith("/") and (target == "AI-Memory" or target.startswith("AI-Memory/"))


def _prefixes(norm):
    import posixpath
    parent = posixpath.dirname(norm)
    while parent:
        yield parent
        parent = posixpath.dirname(parent)


def unsafe_members(tf):
    """Names of tar members that must never be extracted from a backup (textual pre-scan).

    Allowed: regular files, directories, symlinks whose relative target stays inside AI-Memory/
    without passing through another symlink member, and hardlinks to an EARLIER regular-file member.
    Refused: absolute or '..' names, members outside AI-Memory/, duplicate names (a later member would
    replace or write through an earlier one), members below a symlink member (the classic chain:
    "a -> .", "a/b -> ..", "a/b/x" escapes even though each link looks contained), escaping/absolute
    symlinks, devices and FIFOs. safe_extract() additionally checks every path on disk.
    """
    import posixpath
    bad, seen, symlinks, regular = [], set(), set(), set()
    for m in tf.getmembers():
        norm = posixpath.normpath(m.name) if m.name else ""
        parts = norm.split("/")
        if (not m.name or m.name.startswith("/") or ".." in Path(m.name).parts or parts[0] != "AI-Memory"
                or norm in seen or any(pre in symlinks for pre in _prefixes(norm))):
            bad.append(m.name)
            continue
        seen.add(norm)
        if m.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(norm), m.linkname))
            through_link = target in symlinks or any(pre in symlinks for pre in _prefixes(target))
            if not _contained_symlink(norm, m.linkname) or through_link:
                bad.append(m.name)
            symlinks.add(norm)
        elif m.islnk():
            if posixpath.normpath(m.linkname) not in regular:
                bad.append(m.name)  # must point at an earlier regular file that is not below a symlink
        elif m.isfile():
            regular.add(norm)
        elif not m.isdir():
            bad.append(m.name)
    return bad


def _within(path, base):
    try:
        Path(os.path.realpath(path)).relative_to(os.path.realpath(base))
        return True
    except ValueError:
        return False


def safe_extract(tf, dest):
    """Extract member by member, re-checking the real on-disk location before and after each one.

    Does not rely on tarfile's extraction filters (absent on older Pythons such as macOS's system
    Python 3.9.6); where the "data" filter exists it is applied as well. Raises UnsafeArchive.
    """
    bad = unsafe_members(tf)
    if bad:
        raise UnsafeArchive(f"refusing {len(bad)} unsafe member(s), e.g. {bad[:3]}")
    dest = str(dest)
    for m in tf.getmembers():
        target = os.path.join(dest, m.name)
        if not _within(os.path.dirname(target), dest):
            raise UnsafeArchive(f"member would be written outside the target: {m.name}")
        if os.path.lexists(target) and not (m.isdir() and os.path.isdir(target) and not os.path.islink(target)):
            raise UnsafeArchive(f"member would overwrite an existing path: {m.name}")
        try:
            tf.extract(m, dest, filter="data")
        except TypeError:  # Python without extraction filters
            tf.extract(m, dest)
        if (m.issym() or m.islnk()) and not _within(target, dest):
            os.unlink(target)
            raise UnsafeArchive(f"link resolves outside the target: {m.name}")


def backup_dir():
    d = Path(config().get("backup_dir", str(Path.home() / "AI-Memory-Backups"))).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _excluded(rel_parts, name):
    """rel_parts: parent directory parts relative to the root; name: the entry itself."""
    parts = tuple(rel_parts) + (name,)
    if parts[0] in EXCLUDE_TOP or ".generated" in parts:
        return True
    if parts[:1] == ("registry",) and len(parts) == 2 and name in EXCLUDE_DB:
        return True
    return name.endswith(".staged") or name.endswith(".tmp")


def _collect(root: Path):
    """Deterministic list of (kind, arcname, path, linkname) for everything a backup must contain,
    plus a list of skipped paths with reasons.

    kind: dir | file | link. In-root symlinks are kept as links (absolute in-root links are rewritten
    relative). Symlinks pointing OUTSIDE the root are dereferenced one level: the content they point
    to is archived at the link's position, because it is memory data a restore must bring back;
    links nested inside such content, dangling links and names containing a newline are skipped and
    listed (never silently)."""
    root_real = os.path.realpath(root)
    out, skipped = [], []

    def add_tree(src_dir, arc_dir, external):
        for dirpath, dirnames, filenames in os.walk(src_dir, onerror=lambda _e: None):
            rel = () if dirpath == str(src_dir) else Path(dirpath).relative_to(src_dir).parts
            arc_here = "/".join((arc_dir,) + rel)
            keep_dirs = []
            for d in sorted(dirnames):
                if not external and _excluded(rel_of(arc_here), d):
                    continue
                keep_dirs.append(d)
            dirnames[:] = [d for d in keep_dirs if not os.path.islink(os.path.join(dirpath, d))]
            for name in sorted(keep_dirs + [f for f in filenames]):
                full = os.path.join(dirpath, name)
                arc = f"{arc_here}/{name}"
                if "\n" in name or "\r" in name:
                    skipped.append((arc, "newline in file name"))
                    continue
                if not external and _excluded(rel_of(arc_here), name):
                    continue
                if os.path.islink(full):
                    add_link(full, arc, external)
                elif os.path.isdir(full):
                    out.append(("dir", arc, full, None))
                elif os.path.isfile(full):
                    out.append(("file", arc, full, None))
                else:
                    skipped.append((arc, "not a regular file or directory"))

    def rel_of(arc_dir):
        return tuple(arc_dir.split("/")[1:])

    def add_link(full, arc, external):
        real = os.path.realpath(full)
        if external:
            skipped.append((arc, "symlink inside externally linked content"))
        elif not os.path.exists(real):
            skipped.append((arc, "dangling symlink"))
        elif _within(real, root_real):
            linkname = os.path.relpath(real, os.path.dirname(os.path.join(root_real, *arc.split("/")[1:])))
            out.append(("link", arc, full, linkname))
        elif _within(root_real, real):
            skipped.append((arc, f"symlink to {real}, an ancestor of the memory root (not archived)"))
        elif os.path.isdir(real):
            skipped.append((arc, f"external directory link dereferenced from {real}"))
            out.append(("dir", arc, real, None))
            add_tree(real, arc, True)
        else:
            skipped.append((arc, f"external file link dereferenced from {real}"))
            out.append(("file", arc, real, None))

    add_tree(root, "AI-Memory", False)
    return out, skipped


def _add_snapshot(tf, path, arcname, manifest):
    """Add one regular file, hashing exactly the bytes archived (a concurrent append cannot make the
    manifest disagree with the archive). Hardlinks are stored as tar hardlinks to the first name."""
    import hashlib
    ti = tf.gettarinfo(path, arcname=arcname)
    if ti.islnk():
        tf.addfile(ti)
        manifest.append((manifest_sha(manifest, ti.linkname), arcname))
        return
    h = hashlib.sha256()
    with open(path, "rb") as src, tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024) as buf:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            h.update(chunk)
            buf.write(chunk)
        ti.size = buf.tell()
        buf.seek(0)
        tf.addfile(ti, buf)
    manifest.append((h.hexdigest(), arcname))


def manifest_sha(manifest, arcname):
    for sha, name in manifest:
        if name == arcname:
            return sha
    return ""


def create(output_dir=None, label=""):
    core.ensure_root()
    outdir = Path(output_dir).expanduser() if output_dir else backup_dir()
    outdir.mkdir(parents=True, exist_ok=True)
    safe = ("-" + "".join(c if c.isalnum() or c in "._-" else "-" for c in label)) if label else ""
    name = f"AI-Memory-{stamp()}{safe}"
    target = outdir / f"{name}.tar.gz"
    tmp = Path(str(target) + ".partial")
    entries, skipped = _collect(ROOT)
    manifest = []
    with tarfile.open(tmp, "w:gz") as tf:
        tf.addfile(tf.gettarinfo(os.path.realpath(ROOT), arcname="AI-Memory"))  # the root may itself be a symlink
        for kind, arc, path, linkname in entries:
            try:
                if kind == "file":
                    _add_snapshot(tf, path, arc, manifest)
                else:
                    ti = tf.gettarinfo(path, arcname=arc)
                    if kind == "link":
                        ti.linkname = linkname
                    tf.addfile(ti)
            except FileNotFoundError:
                skipped.append((arc, "vanished while the backup was being written"))
        text = "".join(f"{sha}  {arc.split('/', 1)[1]}\n" for sha, arc in manifest)
        data = text.encode("utf-8")
        ti = tarfile.TarInfo("AI-Memory/BACKUP_MANIFEST.sha256")
        ti.size, ti.mtime, ti.mode = len(data), int(time.time()), 0o600
        import io
        tf.addfile(ti, io.BytesIO(data))
    with tmp.open("rb") as f:
        os.fsync(f.fileno())
    os.replace(tmp, target)
    core.atomic_write(sidecar(target, "sha256"), f"{sha256_file(target)}  {target.name}\n")
    core.atomic_write(sidecar(target, "meta.json"), core.dumps({
        "created": iso(), "engine_version": core.VERSION, "root": str(ROOT), "label": label,
        "bytes": target.stat().st_size, "sha256": sha256_file(target), "manifest_entries": len(manifest),
        "skipped_or_dereferenced": [{"path": a, "reason": r} for a, r in skipped[:500]]}))
    deref = [a for a, r in skipped if "dereferenced" in r]
    lost = [a for a, r in skipped if "dereferenced" not in r]
    if deref:
        print(f"WARN: backup archived the content of {len(deref)} symlink(s) pointing outside the memory root "
              f"(restored as real files; listed in {sidecar(target, 'meta.json').name})", file=sys.stderr)
    if lost:
        print(f"WARN: backup skipped {len(lost)} path(s) it cannot archive safely "
              f"(listed in {sidecar(target, 'meta.json').name})", file=sys.stderr)
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
        try:
            with tarfile.open(bp, "r:gz") as tf:
                safe_extract(tf, tmp)
        except (UnsafeArchive, tarfile.TarError, OSError, EOFError) as e:
            report["checks"].append(("tar_paths_safe", f"FAIL {type(e).__name__}: {str(e)[:300]}"))
            return _finish_verify(bp, report)
        report["checks"].append(("tar_paths_safe", "PASS"))
        root = tmp / "AI-Memory"
        man = root / "BACKUP_MANIFEST.sha256"
        if man.exists():
            bad = 0
            total = 0
            for line in man.read_text().splitlines():
                if not line.strip():
                    continue
                if "  " not in line:
                    bad += 1
                    total += 1
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
    ROOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="aimem-restore-", dir=str(ROOT.parent)))
    try:
        with tarfile.open(bp, "r:gz") as tf:
            safe_extract(tf, tmp)  # re-validated: the file could have changed since verify()
    except (UnsafeArchive, tarfile.TarError, OSError, EOFError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        core.die(f"Restore refused: extraction failed ({type(e).__name__}: {e}); live root untouched")
    extracted = tmp / "AI-Memory"
    if not (extracted / "registry" / "projects.json").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        core.die("Restore refused: extracted backup has no registry/projects.json")
    aside = None
    if os.path.lexists(ROOT):  # a missing root (the disaster case) is simply recreated
        aside = ROOT.parent / f"{ROOT.name}.pre-restore-{stamp()}"
        os.rename(ROOT, aside)
    try:
        os.rename(extracted, ROOT)
    except OSError as e:
        if aside is not None and not os.path.lexists(ROOT):
            os.rename(aside, ROOT)  # put the previous root back rather than leave no root at all
        core.die(f"Restore failed while swapping roots ({e}); previous root is {'back in place' if aside else 'absent'}, "
                 f"extracted copy kept at {extracted}")
    shutil.rmtree(tmp, ignore_errors=True)
    return aside
