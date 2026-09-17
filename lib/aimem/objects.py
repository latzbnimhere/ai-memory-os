"""Content-addressed artifact storage: objects/sha256/ab/cd/<fullhash>

Identical content is stored once. Metadata (ARTIFACTS.jsonl, knowledge imports)
points at the object by hash. Objects are read-only files; `verify` recomputes hashes.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import core
from .core import OBJECTS, sha256_file


def object_path(sha):
    return OBJECTS / "sha256" / sha[:2] / sha[2:4] / sha


def store_file(src: Path):
    """Returns (sha256, object_path, deduplicated: bool)."""
    src = Path(src)
    sha = sha256_file(src)
    dst = object_path(sha)
    if dst.exists():
        return sha, dst, True
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{sha}.tmp.{os.getpid()}"
    try:
        shutil.copyfile(src, tmp)
        with tmp.open("rb") as f:
            os.fsync(f.fileno())
        if sha256_file(tmp) != sha:
            core.die(f"Object copy hash mismatch for {src}")
        os.chmod(tmp, 0o444)
        os.replace(tmp, dst)
        core._fsync_dir(dst.parent)
    finally:
        if tmp.exists():
            tmp.unlink()
    return sha, dst, False


def store_text(text: str):
    sha = core.sha256_text(text)
    dst = object_path(sha)
    if dst.exists():
        return sha, dst, True
    dst.parent.mkdir(parents=True, exist_ok=True)
    core.atomic_write(dst, text, mode=0o444)
    return sha, dst, False


def iter_objects():
    base = OBJECTS / "sha256"
    if not base.exists():
        return
    for a in sorted(base.iterdir()):
        if not a.is_dir():
            continue
        for b in sorted(a.iterdir()):
            if not b.is_dir():
                continue
            for f in sorted(b.iterdir()):
                if f.is_file() and not f.name.startswith("."):
                    yield f


def verify_all():
    """Returns (ok_count, [(path, expected, actual)] mismatches, [misplaced])."""
    ok, bad, misplaced = 0, [], []
    for f in iter_objects():
        expected = f.name
        if f.parent.name != expected[2:4] or f.parent.parent.name != expected[:2]:
            misplaced.append(str(f))
        actual = sha256_file(f)
        if actual != expected:
            bad.append((str(f), expected, actual))
        else:
            ok += 1
    return ok, bad, misplaced


def stats():
    count = 0
    size = 0
    for f in iter_objects():
        count += 1
        try:
            size += f.stat().st_size
        except OSError:
            pass
    return {"objects": count, "bytes": size}


def referenced_hashes():
    """All object hashes referenced by project metadata."""
    refs = {}
    for slug in core.all_slugs():
        p = core.project_dir(slug)
        for rec in core.read_jsonl(p / "ARTIFACTS.jsonl"):
            if rec.get("object"):
                refs.setdefault(rec["object"], []).append(f"{slug}:ARTIFACTS.jsonl")
        for rec in core.read_jsonl(p / "EVENTS.jsonl"):
            if rec.get("object"):
                refs.setdefault(rec["object"], []).append(f"{slug}:EVENTS.jsonl")
    return refs


def broken_references():
    out = []
    for sha, where in referenced_hashes().items():
        if not object_path(sha).exists():
            out.append((sha, where))
    return out
