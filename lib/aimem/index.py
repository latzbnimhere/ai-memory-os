"""SQLite FTS5 derived index with local deterministic re-ranking.

Ranking signals (all local, no embeddings):
  - bm25 relevance (FTS5)
  - kind authority weight (CURRENT/NEXT > checkpoint > decision/provenance > summaries > knowledge > events > cold journals)
  - recency (mtime decay over 30 days)
  - current-checkpoint authority (+)
  - verified provenance (+)
  - exact phrase match (+)
  - checkpoint/phase label match (+)
Project scope is enforced by the WHERE clause. Ties are broken by (path, chunk_no), so the
same index and query always produce the same order.

The index is derived and disposable. It is refreshed incrementally (per-file size/mtime,
then sha256) and can always be rebuilt from canonical files with `aimem reindex --full`.
"""
from __future__ import annotations

import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from . import core
from .core import DB, GENERATED_IGNORE, ROOT, TEXT_EXTS, config, project_dir, registry, sha256_file

KIND_WEIGHT = {
    "hot_current": 3.0, "hot_next": 2.8, "checkpoint": 1.9, "decision": 1.6, "provenance": 1.5,
    "summary": 1.45, "knowledge": 1.2, "chat_import": 1.1, "event": 1.0, "artifact_text": 0.9,
    "project_text": 0.9, "step_journal": 0.7, "physical_journal": 0.6, "txn_journal": 0.5,
}


# ---------------------------------------------------------------- health classification
#
# SQLite reports a damaged database file in two equally legitimate ways, and which one you get
# for the SAME bytes depends on the SQLite build (version, compile options, platform):
#   - PRAGMA integrity_check completes and returns problem rows      -> "CORRUPT:<first row>"
#   - the check itself raises (malformed image, not a database,
#     string or blob too big, ...)                                     -> "UNREADABLE:<error>"
# Both mean "this derived index is broken: quarantine and rebuild". Anything that only means the
# file is temporarily inaccessible (locked/busy) is "UNAVAILABLE:<error>" and is NOT broken:
# repairing it would quarantine a healthy index. Callers must use db_is_broken(), never a prefix.
DB_OK = "OK"
DB_MISSING = "MISSING_REBUILDABLE"
DB_BROKEN_PREFIXES = ("CORRUPT:", "UNREADABLE:")
DB_UNAVAILABLE_PREFIX = "UNAVAILABLE:"
_CORRUPTION_MARKERS = ("malformed", "not a database", "disk image", "file is encrypted", "corrupt", "too big",
                       "database schema", "no such table", "no such column", "vtable constructor failed")


def db_is_broken(status):
    return str(status or "").startswith(DB_BROKEN_PREFIXES)


def is_corruption_error(exc):
    """True when an sqlite3 exception means the index file itself is damaged (rebuild), False for
    transient conditions (locked/busy) and for API misuse (ProgrammingError = a bug to surface)."""
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.NotSupportedError)):
        return False
    msg = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError):
        return any(m in msg for m in _CORRUPTION_MARKERS)
    return isinstance(exc, sqlite3.DatabaseError)  # DatabaseError/DataError/IntegrityError/InternalError


def classify_integrity(rows=None, exc=None):
    """Map an integrity_check outcome (its rows, or the exception it raised) to a health status."""
    if exc is not None:
        if isinstance(exc, sqlite3.Error) and not is_corruption_error(exc):
            return f"{DB_UNAVAILABLE_PREFIX}{exc}"
        return f"UNREADABLE:{exc}"
    rows = [r[0] for r in (rows or [])]
    if rows == ["ok"]:
        return DB_OK
    return f"CORRUPT:{rows[0] if rows else 'integrity_check returned no rows'}"


def _quarantine_db(reason):
    """The FTS index is derived and rebuildable: move a corrupt file aside, never delete it.
    Caller holds the "index" lock."""
    tag = f"{core.stamp()}-{os.getpid()}-{time.time_ns() % 1000000:06d}"  # unique: never overwrite an earlier quarantine
    for suf in ("", "-wal", "-shm"):
        q = Path(str(DB) + suf)
        if q.exists():
            q.rename(Path(str(DB) + f".corrupt-{tag}" + suf))
    print(f"WARN: memory.db quarantined as memory.db.corrupt-{tag} ({reason}); index will be rebuilt", file=sys.stderr)


SCHEMA_VERSION = 2  # PRAGMA user_version of the derived index; bump to force a clean rebuild


def _open():
    con = sqlite3.connect(str(DB), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def _create_schema(con):
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks_meta(
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
            chunk_no INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime REAL NOT NULL)
    """)
    con.execute("CREATE INDEX IF NOT EXISTS chunks_meta_path ON chunks_meta(project, path)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS files(
            project TEXT NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL, chunks INTEGER NOT NULL, PRIMARY KEY(project, path))
    """)
    try:
        con.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                body, project UNINDEXED, path UNINDEXED, kind UNINDEXED, chunk_no UNINDEXED, mtime UNINDEXED,
                tokenize='unicode61')
        """)
    except sqlite3.OperationalError as e:
        con.close()
        core.die(f"SQLite FTS5 is unavailable: {e}")


def db_connect(migrate=True):
    """Open (creating/upgrading) the derived index. Older layouts are dropped and rebuilt:
    the index is disposable, canonical files are the source of truth.

    Schema changes happen only with migrate=True, which callers use while holding the "index"
    lock. With migrate=False an out-of-date or missing schema returns None (caller treats the
    index as empty) instead of dropping tables unlocked under a concurrent reindex."""
    core.ensure_root()
    if not migrate:
        if not DB.exists():
            return None
        con = _open()
        if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            con.close()
            return None
        return con
    try:
        con = _open()
        ver = con.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
        if not is_corruption_error(e):
            raise
        _quarantine_db(str(e))
        con = _open()
        ver = 0
    if ver != SCHEMA_VERSION:
        for t in ("chunks_fts", "chunks_meta", "files"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        _create_schema(con)
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
    else:
        _create_schema(con)
    return con


def split_chunks(text: str, max_chars=3500):
    text = text.replace("\x00", "")
    lines = text.splitlines()
    chunks, buf, size = [], [], 0
    for line in lines:
        boundary = (line.startswith("#") or not line.strip()) and size >= 1400
        if boundary or size + len(line) + 1 > max_chars:
            if buf:
                chunks.append("\n".join(buf).strip())
            buf, size = [], 0
        buf.append(line)
        size += len(line) + 1
    if buf:
        chunks.append("\n".join(buf).strip())
    return [c for c in chunks if c]


def classify_path(rel: Path):
    s = str(rel)
    if rel.name == "CURRENT.md":
        return "hot_current"
    if rel.name == "NEXT.md":
        return "hot_next"
    if rel.name == "DECISIONS.jsonl":
        return "decision"
    if rel.name == "PROVENANCE.jsonl":
        return "provenance"
    if rel.name == "EVENTS.jsonl":
        return "event"
    if "/checkpoints/" in s:
        return "checkpoint"
    if "/cold/summaries/" in s:
        return "summary"
    if "/cold/step-journal/" in s:
        return "step_journal"
    if "/cold/physical-journal/" in s:
        return "physical_journal"
    if "/cold/txn-journal/" in s:
        return "txn_journal"
    if "/knowledge/chat-imports/" in s:
        return "chat_import"
    if "/knowledge/" in s:
        return "knowledge"
    if "/artifacts/" in s:
        return "artifact_text"
    return "project_text"


def _walk_files(root):
    """Sorted regular files under root. Tolerates entries vanishing mid-walk (a concurrent rmtree of a
    refused checkpoint, a manual archive) instead of raising; never descends into symlinked dirs
    (same as the previous rglob) or into ignored/session trees."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda _e: None):
        rel = Path(dirpath).relative_to(root).parts
        dirnames[:] = sorted(d for d in dirnames if d not in GENERATED_IGNORE and not (not rel and d == "sessions"))
        for fn in sorted(filenames):
            f = Path(dirpath) / fn
            try:
                if f.is_file():
                    yield f
            except OSError:
                continue


def iter_index_files(slug=None):
    cfg = config()
    roots = [(slug, project_dir(slug))] if slug else [(s, project_dir(s)) for s in sorted(registry()["projects"])]
    for s, root in roots:
        if not root.exists():
            continue
        for f in _walk_files(root):
            parts = f.relative_to(root).parts
            if f.name.startswith(".") or f.name.endswith(".staged"):
                continue
            if f.suffix.lower() not in TEXT_EXTS:
                continue
            if parts[0] == "checkpoints" and len(parts) > 2 and not (root / "checkpoints" / parts[1] / "meta.json").exists():
                continue  # incomplete checkpoint (interrupted before sealing): never authoritative
            try:
                if f.stat().st_size > cfg["max_index_file_bytes"]:
                    continue
            except OSError:
                continue
            yield s, f


def _delete_path(con, project, rel):
    ids = [r[0] for r in con.execute("SELECT id FROM chunks_meta WHERE project=? AND path=?", (project, rel))]
    for i in range(0, len(ids), 500):
        batch = ids[i:i + 500]
        con.execute(f"DELETE FROM chunks_fts WHERE rowid IN ({','.join('?' * len(batch))})", batch)
    con.execute("DELETE FROM chunks_meta WHERE project=? AND path=?", (project, rel))
    con.execute("DELETE FROM files WHERE project=? AND path=?", (project, rel))


def _refresh(con, slug=None, full=False, verify_fts=True):
    """Bring the index in line with the files on disk. Returns (files_in_scope, chunks_in_scope, changed, removed).

    A file is re-read only when its size or mtime changed, and re-chunked only when its sha256
    changed, so refreshing a project with a huge history costs one stat() per file.
    """
    scope = [slug] if slug else sorted(registry()["projects"])
    for s in scope:
        # Consistency guard: an older engine (e.g. after a rollback) may have rewritten chunks without
        # maintaining `files`; any disagreement means the bookkeeping cannot be trusted -> rebuild project.
        # (files and chunks_meta are indexed lookups; the FTS count is a table scan, so the per-search
        # refresh skips it and reindex/finish perform it.)
        tracked = con.execute("SELECT COALESCE(SUM(chunks), 0) FROM files WHERE project=?", (s,)).fetchone()[0]
        actual = con.execute("SELECT COUNT(*) FROM chunks_meta WHERE project=?", (s,)).fetchone()[0]
        fts = con.execute("SELECT COUNT(*) FROM chunks_fts WHERE project=?", (s,)).fetchone()[0] if verify_fts else actual
        if full or tracked != actual or actual != fts:
            for t in ("chunks_meta", "chunks_fts", "files"):
                con.execute(f"DELETE FROM {t} WHERE project=?", (s,))
    if not slug:
        # projects no longer registered
        known = set(scope)
        for (s,) in con.execute("SELECT DISTINCT project FROM files").fetchall():
            if s not in known:
                for t in ("chunks_meta", "chunks_fts", "files"):
                    con.execute(f"DELETE FROM {t} WHERE project=?", (s,))
    indexed = {}
    for s in scope:
        for path, size, mtime_ns, sha in con.execute("SELECT path, size, mtime_ns, sha256 FROM files WHERE project=?", (s,)):
            indexed[(s, path)] = (size, mtime_ns, sha)
    seen = set()
    files = changed = 0
    for s, f in iter_index_files(slug):
        try:
            st = f.stat()
            rel = str(f.relative_to(ROOT))
        except (OSError, ValueError):
            continue
        key = (s, rel)
        seen.add(key)
        files += 1
        old = indexed.get(key)
        if old and old[0] == st.st_size and old[1] == st.st_mtime_ns:
            continue
        try:
            raw = f.read_text(errors="ignore")
            sha = sha256_file(f)
        except OSError:
            continue
        if old and old[2] == sha:
            con.execute("UPDATE files SET size=?, mtime_ns=? WHERE project=? AND path=?", (st.st_size, st.st_mtime_ns, s, rel))
            continue
        changed += 1
        _delete_path(con, s, rel)
        kind = classify_path(Path(rel))
        chunks = split_chunks(raw)
        for i, body in enumerate(chunks):
            cur = con.execute("INSERT INTO chunks_meta(project,path,kind,chunk_no,sha256,mtime) VALUES(?,?,?,?,?,?)",
                              (s, rel, kind, i, sha, st.st_mtime))
            con.execute("INSERT INTO chunks_fts(rowid,body,project,path,kind,chunk_no,mtime) VALUES(?,?,?,?,?,?,?)",
                        (cur.lastrowid, body, s, rel, kind, i, st.st_mtime))
        con.execute("INSERT OR REPLACE INTO files(project,path,size,mtime_ns,sha256,chunks) VALUES(?,?,?,?,?,?)",
                    (s, rel, st.st_size, st.st_mtime_ns, sha, len(chunks)))
    removed = 0
    for key in indexed:
        if key not in seen:
            _delete_path(con, *key)
            removed += 1
    con.commit()
    q = "SELECT COUNT(*) FROM chunks_meta" + (" WHERE project=?" if slug else "")
    chunks_total = con.execute(q, (slug,) if slug else ()).fetchone()[0]
    return files, chunks_total, changed, removed


def _locked_refresh(slug=None, full=False, verify_fts=True):
    """Caller holds the "index" lock."""
    con = db_connect()
    try:
        return _refresh(con, slug, full=full, verify_fts=verify_fts)
    finally:
        con.close()


def reindex(slug=None, quiet=False, full=False, best_effort=False):
    """Incrementally refresh the derived index (full=True rebuilds the scope from scratch).

    best_effort=True is for callers that already committed canonical state (finish, checkpoint,
    imports): the derived index must never turn a committed write into a failure. If the index is
    busy or the refresh hits a non-corruption SQLite error, it warns and returns None; the next
    search/reindex refreshes incrementally anyway.
    """
    core.ensure_root()
    if slug and not project_dir(slug).exists():
        core.die(f"Unknown project: {slug}")
    with core.lock("index", timeout=120 if not best_effort else 30, required=not best_effort) as got:
        if not got:
            print("WARN: INDEX_REFRESH_DEFERRED (index busy); canonical state is committed; next search refreshes it",
                  file=sys.stderr)
            return None
        try:
            try:
                files, chunks, changed, removed = _locked_refresh(slug, full=full)
            except sqlite3.DatabaseError as e:
                if not is_corruption_error(e):
                    raise
                # corrupt derived index: quarantine (never delete) and rebuild everything from canonical files
                _quarantine_db(str(e))
                _locked_refresh(None, full=True)
                files, chunks, changed, removed = _locked_refresh(slug)
        except (sqlite3.Error, OSError) as e:
            if not best_effort:
                raise
            print(f"WARN: INDEX_REFRESH_DEFERRED ({type(e).__name__}: {e}); canonical state is committed",
                  file=sys.stderr)
            return None
    if not quiet:
        print(f"REINDEX=PASS files={files} chunks={chunks} changed={changed} removed={removed} "
              f"mode={'full' if full else 'incremental'} db={DB}")
    return files, chunks


def search_terms(query):
    toks = re.findall(r"[\w./:-]{2,}", query or "", flags=re.UNICODE)
    return toks[:24]


def _rerank(rows, query, slug, limit):
    man = core.try_load_json(project_dir(slug) / "project.json", {}) or {}
    cur_cp = man.get("current_checkpoint") or ""
    q = (query or "").strip().lower()
    terms = [t.lower() for t in search_terms(query)]
    now = time.time()
    scored = []
    for r in rows:
        path, kind, chunk_no, snip, bm25, body, mtime = r
        rel = -float(bm25 or 0.0)  # bm25() in FTS5 is negative-better; flip to positive
        score = rel * KIND_WEIGHT.get(kind, 1.0)
        age_days = max(0.0, (now - float(mtime or 0)) / 86400.0)
        score *= 1.0 + 0.5 * math.exp(-age_days / 30.0)
        if cur_cp and cur_cp in path:
            score += 0.6 * max(rel, 1.0)
        if kind == "provenance" and '"status": "VERIFIED"' in body:
            score += 0.3 * max(rel, 1.0)
        lb = body.lower()
        if q and len(q) >= 4 and q in lb:
            score += 0.8 * max(rel, 1.0)
        if kind == "checkpoint":
            label = path.lower()
            if terms and any(t in label for t in terms):
                score += 0.3 * max(rel, 1.0)
        scored.append({"path": path, "kind": kind, "chunk_no": chunk_no, "snippet": snip, "rank": -score, "score": score, "body": body})
    # deterministic: equal scores are ordered by path then chunk number
    scored.sort(key=lambda x: (-round(x["score"], 9), x["path"], x["chunk_no"]))
    return scored[:limit]


def _query(slug, match, limit):
    con = db_connect(migrate=False)
    if con is None:
        return []
    try:
        return con.execute("""
            SELECT path, kind, chunk_no, snippet(chunks_fts,0,'[[',']]',' … ',42) AS snip,
                   bm25(chunks_fts) AS rank, body, mtime
            FROM chunks_fts WHERE chunks_fts MATCH ? AND project=? ORDER BY rank, path, chunk_no LIMIT ?
        """, (match, slug, int(limit) * 6)).fetchall()
    finally:
        con.close()


def fts_search(slug, query, limit=12, refresh=True):
    """Ranked project-scoped search. Refreshes the project's index incrementally first (cheap: one
    stat per file) so results include journal entries written since the last finish. If another
    process holds the index lock, searches the existing index rather than waiting."""
    terms = search_terms(query)
    if not terms:
        return []
    match = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
    try:
        if refresh:
            with core.lock("index", timeout=3, required=False) as got:
                if got:
                    _locked_refresh(slug, verify_fts=False)
        rows = _query(slug, match, limit)
    except sqlite3.DatabaseError as e:
        if is_corruption_error(e):
            rows = _recover_and_query(slug, match, limit, e)
        else:
            rows = []  # malformed MATCH expression or a busy index: no results rather than a crash
    return _rerank(rows, query, slug, limit)


def _recover_and_query(slug, match, limit, err):
    """Corrupt derived index: quarantine (never delete), rebuild from canonical files, retry once.

    Never blocks begin/search behind another process: if the index lock is busy (someone else is
    probably rebuilding it), return no retrieval results now; the canonical sections of the context
    are unaffected and `aimem doctor` reports whatever damage remains."""
    try:
        with core.lock("index", timeout=5, required=False) as got:
            if not got:
                print("WARN: search index damaged and busy; retrieval skipped for this call", file=sys.stderr)
                return []
            try:
                return _query(slug, match, limit)  # healed concurrently while we waited for the lock?
            except sqlite3.DatabaseError as e:
                if not is_corruption_error(e):
                    return []
            _quarantine_db(str(err))
            _locked_refresh(None, full=True)
        return _query(slug, match, limit)
    except (sqlite3.Error, OSError):
        return []  # never crash begin/search on a derived index; doctor reports what is left


def db_health():
    """OK | MISSING_REBUILDABLE | CORRUPT:<row> | UNREADABLE:<error> | UNAVAILABLE:<error>.
    Use db_is_broken() to decide whether it needs quarantine + rebuild (see classify_integrity)."""
    if not DB.exists():
        return DB_MISSING
    con = None
    try:
        con = sqlite3.connect(str(DB), timeout=30)
        status = classify_integrity(rows=con.execute("PRAGMA integrity_check(20)").fetchall())
        if status == DB_OK and con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = [t for t in ("chunks_fts", "chunks_meta", "files") if t not in names]
            if missing:
                return f"CORRUPT:schema missing table(s) {', '.join(missing)}"
        return status
    except Exception as e:  # noqa: BLE001 - any failure to even check is a classified result, never a crash
        return classify_integrity(exc=e)
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass
