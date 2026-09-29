"""Stable Google Drive identity and server-acknowledged readback through the DriveFS desktop client.

Identity (scheme drivefs-item-id-v1)
    The Google account named by the CloudStorage mount (``GoogleDrive-<account>``) plus server-assigned Google Drive
    file ids, read from the DriveFS extended attribute ``com.google.drivefs.item-id#S``. File ids survive DriveFS
    remounts and Mac reboots and differ for any other folder (a same-named copy, another account). ``st_dev`` and
    ``st_ino`` do NOT survive a remount (a reboot changes st_dev; that is how device-number identity broke the bridge), so
    this module never uses them as identity.

Cloud acknowledgement (remote readback)
    DriveFS keeps the server's metadata for every item (file id, size, md5Checksum) in its local SQLite metadata
    database. The live database keeps recent rows in a WAL that other processes cannot read in place, so a private
    APFS clone of the database plus a copy of its WAL is opened instead. Nothing in the DriveFS directory is written.
    A file is acknowledged only when the exact path, resolved from a pinned folder id, names exactly one live item with
    a server-assigned id, the local size and the local md5. Anything else (missing or changed schema, ambiguous names,
    unparseable records, clone failure) is UNVERIFIED, never a pass. No network call and no Google API is used.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

SCHEME = "drivefs-item-id-v1"
ITEM_XATTR = "com.google.drivefs.item-id#S"
ITEM_ID = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
HEX32 = re.compile(rb"^[0-9a-f]{32}$")
# Mirror mode (File Provider) keeps cloud metadata in mirror_metadata_sqlite.db; the legacy stream-mode database is
# only consulted when no mirror database exists for an account.
DB_NAMES = ("mirror_metadata_sqlite.db", "metadata_sqlite_db")


class DriveIdentityError(Exception):
    def __init__(self, code, detail=""):
        self.code = code
        self.detail = detail
        super().__init__(code + (": " + detail if detail else ""))


def cloudstorage_dir():
    return Path(os.environ.get("AIMEM_CLOUDSTORAGE_DIR") or Path.home() / "Library" / "CloudStorage")


def drivefs_dir():
    return Path(os.environ.get("AIMEM_DRIVEFS_DIR") or Path.home() / "Library" / "Application Support" / "Google" / "DriveFS")


def valid_item_id(v):
    return isinstance(v, str) and bool(ITEM_ID.fullmatch(v)) and not v.lower().startswith("local")


def item_id(path):
    """Server-assigned Google Drive id of a DriveFS folder, or None (not synced yet, not DriveFS, unreadable)."""
    try:
        r = subprocess.run(["/usr/bin/xattr", "-p", ITEM_XATTR, str(path)], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    v = r.stdout.strip()
    return v if r.returncode == 0 and valid_item_id(v) else None


def my_drive_of(path):
    """The ``GoogleDrive-<account>/My Drive`` ancestor of path (or path itself), else None."""
    p = Path(path)
    for cand in (p, *p.parents):
        if cand.name == "My Drive" and cand.parent.name.startswith("GoogleDrive-"):
            return cand
    return None


def account_of(path):
    md = my_drive_of(path)
    if md is None:
        return None
    n = md.parent.name[len("GoogleDrive-"):]
    return n or None


def identity(path):
    p = Path(path)
    md = my_drive_of(p)
    return {"identity_scheme": SCHEME, "account": account_of(p), "item_id": item_id(p),
            "my_drive_item_id": item_id(md) if md else None}


def _no_symlinks(p):
    md = my_drive_of(p)
    if md is None:
        return False
    cur = Path(p)
    while True:
        if cur.is_symlink():
            return False
        if cur == md:
            return True
        cur = cur.parent


def check_folder(path, expected, name=None):
    """Fail closed unless path is the pinned Drive folder. expected: {'account','item_id','my_drive_item_id'}."""
    p = Path(path)
    if not p.is_dir():
        raise DriveIdentityError("DRIVE_MISSING", str(p))
    if not _no_symlinks(p) or (name is not None and p.name != name):
        raise DriveIdentityError("DRIVE_IDENTITY_MISMATCH", "path shape")
    if expected.get("identity_scheme") != SCHEME:
        raise DriveIdentityError("DRIVE_IDENTITY_UNVERIFIABLE", "legacy identity scheme; re-pin required")
    got = identity(p)
    if not (got["account"] and got["item_id"] and got["my_drive_item_id"]):
        raise DriveIdentityError("DRIVE_IDENTITY_UNVERIFIABLE", "Drive item id unreadable")
    for k in ("account", "item_id", "my_drive_item_id"):
        if got[k] != expected.get(k):
            raise DriveIdentityError("DRIVE_IDENTITY_MISMATCH", k)
    return p


def wait_item_id(path, timeout=120.0, interval=2.0):
    """New DriveFS folders receive their server id only after upload; poll until it appears."""
    deadline = time.time() + timeout
    while True:
        v = item_id(path)
        if v or time.time() >= deadline:
            return v
        time.sleep(interval)


# ------------------------------------------------------------------ server metadata (cloud acknowledgement)

def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _varint(buf, i):
    shift = result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 63:
            raise ValueError("varint")


def proto_md5s(buf):
    """Top-level length-delimited 32-hex strings of a DriveFS item record (the server md5Checksum lives there)."""
    out = set()
    if not buf:
        return out
    i, n = 0, len(buf)
    try:
        while i < n:
            key, i = _varint(buf, i)
            wt = key & 7
            if wt == 0:
                _, i = _varint(buf, i)
            elif wt == 1:
                i += 8
            elif wt == 5:
                i += 4
            elif wt == 2:
                ln, i = _varint(buf, i)
                v = bytes(buf[i:i + ln])
                i += ln
                if len(v) == 32 and HEX32.fullmatch(v):
                    out.add(v.decode())
            else:
                return set()
        if i != n:
            return set()
    except (IndexError, ValueError):
        return set()
    return out


class CloudIndex:
    """Read-only snapshot of DriveFS server metadata, scoped to one pinned folder id."""

    def __init__(self, root_item_id, base=None, scratch=None):
        self.root_item_id = root_item_id
        self.base = Path(base) if base else drivefs_dir()
        self.scratch = Path(scratch) if scratch else None
        self.tmp = None
        self.conn = None
        self.root_stable = None
        self.source = None
        self.error = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    def _snapshot(self, db):
        """APFS clone of db + copy of its WAL, retried while DriveFS checkpoints concurrently."""
        wal = Path(str(db) + "-wal")
        for _attempt in range(4):
            before = (db.stat().st_mtime_ns, db.stat().st_size)
            tmp = Path(tempfile.mkdtemp(prefix="drivefs-snap-", dir=str(self.scratch) if self.scratch else None))
            try:
                r = subprocess.run(["/bin/cp", "-c", str(db), str(tmp / "m.db")], capture_output=True, timeout=30)
                if r.returncode != 0:
                    raise OSError("clone failed (not APFS?)")
                if wal.exists():
                    shutil.copyfile(wal, tmp / "m.db-wal")
                after = (db.stat().st_mtime_ns, db.stat().st_size)
                if before != after:
                    shutil.rmtree(tmp, ignore_errors=True)
                    time.sleep(0.3)
                    continue
                conn = sqlite3.connect(str(tmp / "m.db"), timeout=5)
                conn.text_factory = bytes
                return conn, tmp
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        raise OSError("database changed during every snapshot attempt")

    def open(self):
        if not valid_item_id(self.root_item_id):
            self.error = "ROOT_ID_INVALID"
            return self
        if not self.base.is_dir():
            self.error = "DRIVEFS_DIR_MISSING"
            return self
        found = []
        for acct in sorted(d for d in self.base.iterdir() if d.is_dir() and d.name.isdigit()):
            for name in DB_NAMES:
                db = acct / name
                if not db.is_file():
                    continue
                try:
                    conn, tmp = self._snapshot(db)
                    row = conn.execute("select stable_id from items where id=? and trashed=0 and is_tombstone=0",
                                       (self.root_item_id,)).fetchone()
                except Exception:
                    self.error = "DRIVEFS_DB_UNREADABLE"
                    continue
                if row:
                    found.append((conn, tmp, row[0], f"{acct.name}/{name}"))
                else:
                    conn.close()
                    shutil.rmtree(tmp, ignore_errors=True)
                break  # the preferred database for this account decides; never mix a stale one in
        if len(found) != 1:
            for conn, tmp, _s, _n in found:
                conn.close()
                shutil.rmtree(tmp, ignore_errors=True)
            self.error = "ROOT_NOT_FOUND" if not found else "ROOT_AMBIGUOUS"
            return self
        self.conn, self.tmp, self.root_stable, self.source = found[0]
        self.error = None
        return self

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
        if self.tmp is not None:
            shutil.rmtree(self.tmp, ignore_errors=True)
        self.conn = self.tmp = None

    def resolve(self, parts):
        """Walk the exact path below the pinned folder. Returns a dict, or {'error': ...}."""
        if self.conn is None:
            return {"error": self.error or "INDEX_CLOSED"}
        sid = self.root_stable
        row = None
        for name in parts:
            rows = self.conn.execute(
                "select i.stable_id, i.id, i.file_size, i.is_folder, i.proto from stable_parents p "
                "join items i on i.stable_id = p.item_stable_id "
                "where p.parent_stable_id=? and i.local_title=? and i.trashed=0 and i.is_tombstone=0",
                (sid, name)).fetchall()
            if not rows:
                return {"error": "NOT_FOUND"}
            if len(rows) > 1:
                return {"error": "AMBIGUOUS"}
            row = rows[0]
            sid = row[0]
        if row is None:
            return {"error": "EMPTY_PATH"}
        fid = row[1].decode() if isinstance(row[1], bytes) else row[1]
        return {"id": fid, "size": row[2], "is_folder": bool(row[3]), "md5s": proto_md5s(row[4])}

    def ack(self, parts, md5, size):
        r = self.resolve(parts)
        if "error" in r:
            return {"status": "UNACKED", "reason": r["error"]}
        if not valid_item_id(r["id"]):
            return {"status": "UNACKED", "reason": "NO_SERVER_ID"}
        if r["is_folder"]:
            return {"status": "UNACKED", "reason": "IS_FOLDER"}
        if r["size"] != size or md5 not in r["md5s"]:
            return {"status": "UNACKED", "reason": "SERVER_CONTENT_DIFFERS", "drive_id": r["id"]}
        return {"status": "ACK", "drive_id": r["id"]}


def wait_cloud_ack(root_item_id, root_path, files, timeout=120.0, interval=3.0, scratch=None, base=None):
    """files: iterable of absolute paths below root_path whose CURRENT local bytes must be on the server.

    Returns {'status': 'ACK'|'UNACKED'|'UNVERIFIABLE', 'files': {relpath: {...}}, 'source': db, 'elapsed_s': n}.
    """
    root_path = Path(root_path)
    want = {}
    for f in files:
        f = Path(f)
        rel = f.relative_to(root_path)
        want[rel.as_posix()] = (rel.parts, md5_file(f), f.stat().st_size)
    t0 = time.time()
    last = {}
    source = None
    while True:
        with CloudIndex(root_item_id, base=base, scratch=scratch) as idx:
            if idx.conn is None:
                last_err = idx.error
                results = None
            else:
                source = idx.source
                results = {rel: idx.ack(parts, md5, size) for rel, (parts, md5, size) in want.items()}
        if results is not None:
            last = results
            if all(r["status"] == "ACK" for r in results.values()):
                return {"status": "ACK", "files": results, "source": source, "elapsed_s": round(time.time() - t0, 1)}
        if time.time() - t0 >= timeout:
            if results is None and not last:
                return {"status": "UNVERIFIABLE", "reason": last_err, "files": {}, "source": None,
                        "elapsed_s": round(time.time() - t0, 1)}
            return {"status": "UNACKED", "files": last, "source": source, "elapsed_s": round(time.time() - t0, 1)}
        time.sleep(interval)
