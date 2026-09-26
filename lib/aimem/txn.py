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


def pending(slug):
    d = txn_dir(slug)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        rec = core.try_load_json(f, None)
        if rec is None:
            out.append({"id": f.stem, "state": "CORRUPT_RECORD", "path": str(f), "entries": []})
        else:
            rec["path"] = str(f)
            out.append(rec)
    return out


def _target_state(e):
    """'PRE' if target still holds its pre-image, 'NEW' if it already holds the new content, else 'CHANGED'."""
    t = Path(e["target"])
    cur = sha256_file(t) if t.exists() else None
    if cur == e.get("pre_sha256"):
        return "PRE"
    if cur is not None and cur == e.get("new_sha256"):
        return "NEW"
    return "CHANGED"


def inspect(slug):
    """Read-only classification of pending transactions."""
    report = []
    for rec in pending(slug):
        state = rec.get("state")
        entries = rec.get("entries", [])
        staged_present = [e for e in entries if os.path.exists(e["staged"])]
        item = {"id": rec.get("id"), "state": state, "kind": rec.get("kind"), "session": rec.get("session"),
                "targets": [e["target"] for e in entries], "staged_present": len(staged_present), "path": rec.get("path")}
        changed = [e["target"] for e in staged_present if _target_state(e) == "CHANGED"]
        if changed:
            item["targets_changed_since_prepare"] = changed
        if state == "PREPARED":
            if not (len(staged_present) == len(entries) and entries):
                item["resolution"] = "ROLL_BACK"
            else:
                item["resolution"] = "MANUAL_TARGET_CHANGED" if changed else "ROLL_FORWARD"
        elif state == "COMMITTING":
            item["resolution"] = "MANUAL_TARGET_CHANGED" if changed else "ROLL_FORWARD"
        elif state == "COMMITTED":
            item["resolution"] = "FINALIZE"
        else:
            item["resolution"] = "MANUAL"
        report.append(item)
    return report


def repair(slug):
    """Apply deterministic recovery. Returns list of (id, action)."""
    actions = []
    with core.project_write_lock(slug):
        for rec in pending(slug):
            rid = rec.get("id")
            state = rec.get("state")
            entries = rec.get("entries", [])
            path = Path(rec.get("path"))
            if state == "CORRUPT_RECORD":
                actions.append((rid, "LEFT_FOR_MANUAL_REVIEW"))
                continue
            present = [e for e in entries if os.path.exists(e["staged"])]
            if state == "PREPARED" and (len(present) != len(entries) or not entries):
                for e in entries:
                    if os.path.exists(e["staged"]):
                        os.unlink(e["staged"])
                path.unlink()
                actions.append((rid, "ROLLED_BACK"))
                continue
            if state in ("PREPARED", "COMMITTING"):
                ok = True
                for e in present:
                    try:
                        if e.get("validate") == "json":
                            core.validate_json_file(Path(e["staged"]))
                        if sha256_file(e["staged"]) != e.get("new_sha256"):
                            ok = False
                    except Exception:
                        ok = False
                if not ok:
                    actions.append((rid, "STAGED_CONTENT_INVALID_MANUAL_REVIEW"))
                    continue
                if any(_target_state(e) == "CHANGED" for e in present):
                    # Newer content was written over a target after the interruption; applying the
                    # stale staged copy would destroy it. Leave everything in place for a human.
                    actions.append((rid, "TARGET_CHANGED_SINCE_PREPARE_MANUAL_REVIEW"))
                    continue
                for e in present:
                    os.replace(e["staged"], e["target"])
                    core._fsync_dir(Path(e["target"]).parent)
                _finalize(slug, path)
                actions.append((rid, "ROLLED_FORWARD"))
                continue
            if state == "COMMITTED":
                _finalize(slug, path)
                actions.append((rid, "FINALIZED"))
                continue
            actions.append((rid, "UNKNOWN_STATE_MANUAL_REVIEW"))
    return actions


def discard(slug, txid):
    """Explicit human decision to abandon one pending transaction: delete its staged files and record.

    Targets already replaced by a COMMITTING transaction are NOT reverted (their pre-images are
    gone); they are reported so the operator can check them. Never automatic.
    """
    with core.project_write_lock(slug):
        for rec in pending(slug):
            if rec.get("id") != txid:
                continue
            entries = rec.get("entries", [])
            applied = [e["target"] for e in entries if not os.path.exists(e.get("staged", ""))]
            for e in entries:
                if os.path.exists(e.get("staged", "")):
                    os.unlink(e["staged"])
            Path(rec["path"]).unlink()
            core.append_jsonl(project_dir(slug) / "cold" / "txn-journal" / f"{core.day()}.jsonl", {
                "time": iso(), "txn": txid, "state": "DISCARDED", "previous_state": rec.get("state"),
                "already_applied_targets": [Path(t).name for t in applied] if rec.get("state") != "PREPARED" else []})
            return rec.get("state"), (applied if rec.get("state") != "PREPARED" else [])
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
