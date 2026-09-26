"""Transactional multi-file canonical writes with detectable/recoverable interruption.

Protocol per transaction (record lives in projects/<slug>/.txn/<txid>.json):

  1. stage:   write each target's new content to <target>.<txid>.staged, fsync, validate
  2. PREPARED record written atomically (lists targets, staged files, pre-image hashes)
  3. record -> COMMITTING
  4. os.replace(staged -> target) for each target in order; fsync dir
  5. record -> COMMITTED, then record removed; compact entry appended to cold/txn-journal

Recovery rules (deterministic):
  PREPARED   : nothing applied yet. Roll back = delete staged files + record.
               Roll forward is also safe if every staged file still exists and validates.
  COMMITTING : partially applied. Roll forward remaining staged files (those still
               present are the un-applied ones). Never roll back (would lose applied data).
  COMMITTED  : finalize (remove record).
Roll-forward is refused (MANUAL review) when a target no longer holds its recorded
pre-image: something rewrote it after the interruption, and blindly applying the old
staged content would destroy newer data.
While any transaction is pending, new canonical updates fail closed (exit 4) so that a
later repair can never roll stale staged content over newer state.
Read-only inspection never mutates; repair must be explicit.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from . import core
from .core import atomic_write, atomic_write_json, iso, project_dir, sha256_file, stamp


def txn_dir(slug):
    return project_dir(slug) / ".txn"


def new_txn_id():
    return f"{stamp()}-{uuid.uuid4().hex[:8]}"


class Transaction:
    def __init__(self, slug, kind="canonical_update", session=None, txn_id=None):
        self.slug = slug
        self.kind = kind
        self.session = session
        self.id = txn_id or new_txn_id()
        self.entries = []  # dicts: target, staged, pre_sha256, validate
        self.record_path = txn_dir(slug) / f"{self.id}.json"
        self.state = "NEW"

    # -- staging
    def stage(self, target: Path, text: str, validate=None):
        target = Path(target)
        base = project_dir(self.slug)
        rel = os.path.relpath(str(target), str(base))
        if os.path.isabs(rel) or rel == ".." or rel.startswith(".." + os.sep):
            core.die(f"transaction target outside project {self.slug}: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.parent / f".{target.name}.{self.id}.staged"
        with staged.open("w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if validate is not None:
            validate(staged)
        pre = sha256_file(target) if target.exists() else None
        self.entries.append({
            "target": str(target),
            "staged": str(staged),
            # project-relative paths: repair resolves these against the CURRENT root, so a moved or
            # copied memory root can never be repaired by writing into the original location
            "target_rel": rel,
            "staged_rel": os.path.relpath(str(staged), str(base)),
            "pre_sha256": pre,
            "new_sha256": sha256_file(staged),
            "validate": "json" if validate is core.validate_json_file else None,
        })

    def stage_json(self, target: Path, obj):
        self.stage(target, core.dumps(obj), validate=core.validate_json_file)

    def _record(self, state):
        self.state = state
        rec = {
            "id": self.id, "project": self.slug, "kind": self.kind, "session": self.session,
            "state": state, "time": iso(), "created_at": getattr(self, "_created", iso()),
            "entries": [{k: v for k, v in e.items()} for e in self.entries],
        }
        self._created = rec["created_at"]
        atomic_write_json(self.record_path, rec)
        return rec

    # -- commit
    def commit(self):
        if not self.entries:
            return None
        self._record("PREPARED")
        self._record("COMMITTING")
        for e in self.entries:
            os.replace(e["staged"], e["target"])
            core._fsync_dir(Path(e["target"]).parent)
        self._record("COMMITTED")
        _finalize(self.slug, self.record_path)
        return self.id

    def abort(self):
        for e in self.entries:
            try:
                if os.path.exists(e["staged"]):
                    os.unlink(e["staged"])
            except OSError:
                pass
        if self.record_path.exists():
            self.record_path.unlink()
        self.state = "ABORTED"


def _finalize(slug, record_path: Path):
    rec = core.try_load_json(record_path, {}) or {}
    core.append_jsonl(project_dir(slug) / "cold" / "txn-journal" / f"{core.day()}.jsonl", {
        "time": iso(), "txn": rec.get("id"), "kind": rec.get("kind"), "session": rec.get("session"),
        "state": "COMMITTED", "targets": [Path(e["target"]).name for e in rec.get("entries", [])],
        "new_sha256": {Path(e["target"]).name: e.get("new_sha256") for e in rec.get("entries", [])},
    })
    try:
        record_path.unlink()
    except OSError:
        pass


def _load_record(f):
    """Parse one record; anything structurally unusable is a CORRUPT_RECORD (never a crash)."""
    rec = core.try_load_json(f, None)
    ok = isinstance(rec, dict) and isinstance(rec.get("entries", []), list) and all(
        isinstance(e, dict) and isinstance(e.get("target"), str) and isinstance(e.get("staged"), str)
        for e in rec.get("entries", []))
    if not ok:
        return {"id": f.stem, "state": "CORRUPT_RECORD", "path": str(f), "entries": []}
    rec.setdefault("id", f.stem)
    rec["path"] = str(f)
    return rec


def pending(slug):
    d = txn_dir(slug)
    if not d.exists():
        return []
    return [_load_record(f) for f in sorted(d.glob("*.json"))]


def _within(path, base):
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def entry_paths(slug, e):
    """(target, staged) for a record entry, resolved against the CURRENT project dir, or None if unsafe.

    Records written by 4.2+ carry project-relative paths. Legacy records only have absolute paths;
    those are accepted only while they still point inside this project (a moved/copied root fails
    closed instead of repairing the original location)."""
    base = project_dir(slug)
    if e.get("target_rel") and e.get("staged_rel"):
        t, st = base / e["target_rel"], base / e["staged_rel"]
    else:
        t, st = Path(e["target"]), Path(e["staged"])
    if not (_within(t, base) and _within(st, base)):
        return None
    return t, st


def _target_state(e, target=None):
    """'PRE' if target still holds its pre-image, 'NEW' if it already holds the new content, else 'CHANGED'."""
    t = Path(target if target is not None else e["target"])
    cur = sha256_file(t) if t.exists() else None
    if cur == e.get("pre_sha256"):
        return "PRE"
    if cur is not None and cur == e.get("new_sha256"):
        return "NEW"
    return "CHANGED"


def _holds(target, sha):
    """True when target currently holds content with this sha256 (None means: target must not exist)."""
    t = Path(target)
    return (sha256_file(t) if t.exists() else None) == sha


def _staged_valid(e, staged):
    try:
        if e.get("validate") == "json":
            core.validate_json_file(Path(staged))
        return sha256_file(staged) == e.get("new_sha256")
    except Exception:  # noqa: BLE001
        return False


def classify(slug, rec):
    """Single source of truth for inspect() and repair(). Returns (resolution, detail dict).

    Resolutions: ROLL_FORWARD, ROLL_BACK, FINALIZE, or MANUAL_* (never applied automatically).
      PREPARED   nothing was applied: roll forward only if every staged file is intact and every target
                 still holds its pre-image; otherwise roll back (safe: discards only unapplied content).
      COMMITTING partially applied: every entry must be consistent -- staged present => target still PRE
                 and staged intact; staged missing => target holds exactly the new content. Anything
                 else means someone changed a target since the crash: MANUAL.
      COMMITTED  all applied: finalize (drop the record).
    """
    state = rec.get("state")
    if state == "CORRUPT_RECORD":
        return "MANUAL_CORRUPT_RECORD", {}
    entries = rec.get("entries", [])
    resolved = [entry_paths(slug, e) for e in entries]
    if any(r is None for r in resolved):
        return "MANUAL_PATHS_OUTSIDE_PROJECT", {}
    present = [(e, t, st) for e, (t, st) in zip(entries, resolved) if st.exists()]
    missing = [(e, t, st) for e, (t, st) in zip(entries, resolved) if not st.exists()]
    detail = {"staged_present": len(present), "never_applied": [str(t) for _e, t, _st in present],
              "applied": [str(t) for _e, t, _st in missing]}
    if state == "COMMITTED":
        return "FINALIZE", detail
    if state == "PREPARED":
        if (entries and not missing and all(_holds(t, e.get("pre_sha256")) for e, t, _ in present)
                and all(_staged_valid(e, st) for e, _t, st in present)):
            return "ROLL_FORWARD", detail
        detail["applied"] = []  # PREPARED never replaced anything
        return "ROLL_BACK", detail
    if state == "COMMITTING":
        # compare each entry with the exact hash it must hold (pre-image may equal the new content)
        changed = [str(t) for e, t, _ in present if not _holds(t, e.get("pre_sha256"))] + \
                  [str(t) for e, t, _ in missing if not _holds(t, e.get("new_sha256"))]
        if changed:
            detail["targets_changed_since_prepare"] = changed
            return "MANUAL_TARGET_CHANGED", detail
        if not all(_staged_valid(e, st) for e, _t, st in present):
            return "MANUAL_STAGED_INVALID", detail
        return "ROLL_FORWARD", detail
    return "MANUAL_UNKNOWN_STATE", detail


def inspect(slug):
    """Read-only classification of pending transactions."""
    report = []
    for rec in pending(slug):
        resolution, detail = classify(slug, rec)
        item = {"id": rec.get("id"), "state": rec.get("state"), "kind": rec.get("kind"), "session": rec.get("session"),
                "targets": [e.get("target") for e in rec.get("entries", [])], "path": rec.get("path"),
                "resolution": resolution, "staged_present": detail.get("staged_present", 0)}
        if detail.get("targets_changed_since_prepare"):
            item["targets_changed_since_prepare"] = detail["targets_changed_since_prepare"]
        report.append(item)
    return report


_MANUAL_ACTIONS = {
    "MANUAL_CORRUPT_RECORD": "LEFT_FOR_MANUAL_REVIEW",
    "MANUAL_PATHS_OUTSIDE_PROJECT": "PATHS_OUTSIDE_PROJECT_MANUAL_REVIEW",
    "MANUAL_TARGET_CHANGED": "TARGET_CHANGED_SINCE_PREPARE_MANUAL_REVIEW",
    "MANUAL_STAGED_INVALID": "STAGED_CONTENT_INVALID_MANUAL_REVIEW",
    "MANUAL_UNKNOWN_STATE": "UNKNOWN_STATE_MANUAL_REVIEW",
}


def repair(slug):
    """Apply deterministic recovery. Returns list of (id, action); actions containing MANUAL need a human."""
    actions = []
    with core.project_write_lock(slug):
        for rec in pending(slug):
            rid = rec.get("id")
            path = Path(rec.get("path"))
            resolution, _detail = classify(slug, rec)
            if resolution in _MANUAL_ACTIONS:
                actions.append((rid, _MANUAL_ACTIONS[resolution]))
                continue
            pairs = [entry_paths(slug, e) for e in rec.get("entries", [])]
            if resolution == "ROLL_BACK":
                for _t, st in pairs:
                    if st.exists():
                        st.unlink()
                path.unlink()
                actions.append((rid, "ROLLED_BACK"))
            elif resolution == "ROLL_FORWARD":
                for t, st in pairs:
                    if st.exists():
                        os.replace(st, t)
                        core._fsync_dir(t.parent)
                _finalize(slug, path)
                actions.append((rid, "ROLLED_FORWARD"))
            elif resolution == "FINALIZE":
                _finalize(slug, path)
                actions.append((rid, "FINALIZED"))
    return actions


def discard(slug, txid, force=False):
    """Explicit human decision to abandon one pending transaction. Never automatic, never destructive:
    the record and every staged file are MOVED to cold/quarantine/txn-<id>/ (not deleted).

    A transaction that `txn --repair` would cleanly roll forward is refused unless force=True, because
    discarding it drops content that was never applied. Targets a COMMITTING transaction had already
    replaced are not reverted; both lists are returned so the operator can check them."""
    with core.project_write_lock(slug):
        for rec in pending(slug):
            if rec.get("id") != txid:
                continue
            resolution, detail = classify(slug, rec)
            if resolution == "ROLL_FORWARD" and not force:
                core.die(f"REFUSED: transaction {txid} can be rolled forward cleanly (`aimem txn {slug} --repair`). "
                         "Discarding it would drop content that was never applied; pass --force to discard anyway.")
            qdir = project_dir(slug) / "cold" / "quarantine" / f"txn-{txid}"
            qdir.mkdir(parents=True, exist_ok=True)
            moved = []
            for e in rec.get("entries", []):
                pair = entry_paths(slug, e)
                if pair and pair[1].exists():
                    dst = qdir / pair[1].name
                    os.replace(pair[1], dst)
                    moved.append(str(dst))
            os.replace(rec["path"], qdir / Path(rec["path"]).name)
            core._fsync_dir(qdir)
            info = {"previous_state": rec.get("state"), "resolution": resolution, "quarantine": str(qdir),
                    "never_applied": detail.get("never_applied", []), "already_applied": detail.get("applied", []),
                    "moved": moved}
            core.append_jsonl(project_dir(slug) / "cold" / "txn-journal" / f"{core.day()}.jsonl", {
                "time": iso(), "txn": txid, "state": "DISCARDED", "forced": bool(force),
                **{k: ([Path(x).name for x in v] if isinstance(v, list) else v) for k, v in info.items()}})
            return info
    core.die(f"No pending transaction {txid} for {slug}")


# ---------------------------------------------------------------- canonical update helper

class VersionConflict(core.AimemError):
    def __init__(self, slug, expected, actual):
        super().__init__(
            f"VERSION_CONFLICT: {slug} memory_version is {actual}, expected {expected}. "
            f"Another agent advanced canonical state; re-read CURRENT.md/NEXT.md and retry with --expect-version {actual}.",
            core.EXIT_CONFLICT)


def require_no_pending(slug):
    """Fail closed (exit 4) while an interrupted transaction is unresolved.

    Writing on top of a half-applied transaction would let a later roll-forward
    overwrite the newer state with stale staged content.
    """
    pend = pending(slug)
    if pend:
        core.die(f"UNRESOLVED_TRANSACTIONS: {slug} has {len(pend)} interrupted transaction(s) "
                 f"({', '.join(str(t.get('id')) for t in pend[:3])}); inspect with `aimem txn {slug}` and "
                 f"resolve with `aimem txn {slug} --repair` before writing canonical state.", core.EXIT_RECOVERY_REQUIRED)


def check_version(slug, expect_version):
    """Current memory_version; raises VersionConflict when expect_version is given and differs."""
    actual = int(core.project_manifest(slug).get("memory_version", 0))
    if expect_version is not None and int(expect_version) != actual:
        raise VersionConflict(slug, expect_version, actual)
    return actual


def canonical_update_locked(slug, files: dict, session=None, expect_version=None, kind="canonical_update",
                            manifest_patch=None, txn_id=None):
    """canonical_update for callers that already hold project_write_lock(slug)."""
    p = project_dir(slug)
    require_no_pending(slug)
    man = core.project_manifest(slug)
    actual = check_version(slug, expect_version)
    man["memory_version"] = actual + 1
    man["updated_at"] = iso()
    man["last_writer_session"] = session
    if manifest_patch:
        man.update(manifest_patch)
    t = Transaction(slug, kind=kind, session=session, txn_id=txn_id)
    try:
        for path, text in files.items():
            path = Path(path)
            validate = core.validate_json_file if path.suffix == ".json" else None
            t.stage(path, text, validate=validate)
        t.stage_json(p / "project.json", man)
        t.commit()
    except BaseException:
        if t.state in ("NEW", "PREPARED"):
            t.abort()  # nothing applied yet: clean rollback
        # COMMITTING/COMMITTED: some targets may already be replaced; keep the record so
        # `aimem txn --repair` can deterministically roll forward.
        raise
    return man["memory_version"], t.id


def canonical_update(slug, files: dict, session=None, expect_version=None, kind="canonical_update", manifest_patch=None,
                     txn_id=None):
    """Atomically write several canonical files + bump project.json memory_version.

    files: {Path: text}. Compare-and-swap on memory_version when expect_version is given.
    Acquires project_write_lock (not re-entrant: use canonical_update_locked if already held).
    """
    with core.project_write_lock(slug):
        return canonical_update_locked(slug, files, session, expect_version, kind, manifest_patch, txn_id)
