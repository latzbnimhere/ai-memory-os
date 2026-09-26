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


def _quarantine_db(reason):
    """The FTS index is derived and rebuildable: move a corrupt file aside, never delete it."""
    tag = core.stamp()
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


def db_connect():
    """Open (creating/upgrading) the derived index. Older layouts are dropped and rebuilt:
    the index is disposable, canonical files are the source of truth."""
    core.ensure_root()
    try:
        con = _open()
        ver = con.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
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


def iter_index_files(slug=None):
    cfg = config()
    roots = [(slug, project_dir(slug))] if slug else [(s, project_dir(s)) for s in sorted(registry()["projects"])]
    for s, root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if not f.is_file():
                continue
            parts = f.relative_to(root).parts
            if any(part in GENERATED_IGNORE for part in parts):
                continue
            if parts and parts[0] == "sessions":
                continue  # session state/steps are retrieved through session commands, not free-text search
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


def _refresh(con, slug=None, full=False):
    """Bring the index in line with the files on disk. Returns (files_in_scope, chunks_in_scope, changed, removed).

    A file is re-read only when its size or mtime changed, and re-chunked only when its sha256
    changed, so refreshing a project with a huge history costs one stat() per file.
    """
    scope = [slug] if slug else sorted(registry()["projects"])
    if full:
        for s in scope:
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


def reindex(slug=None, quiet=False, full=False):
    """Incrementally refresh the derived index (full=True rebuilds the scope from scratch)."""
    core.ensure_root()
    if slug and not project_dir(slug).exists():
        core.die(f"Unknown project: {slug}")
    with core.lock("index", timeout=120):
        con = db_connect()
        try:
            files, chunks, changed, removed = _refresh(con, slug, full=full)
        finally:
            con.close()
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
    con = db_connect()
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
    if refresh:
        with core.lock("index", timeout=3, required=False) as got:
            if got:
                con = db_connect()
                try:
                    _refresh(con, slug)
                finally:
                    con.close()
    match = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
    try:
        rows = _query(slug, match, limit)
    except sqlite3.OperationalError:
        rows = []  # malformed MATCH expression: no results rather than a crash
    except sqlite3.DatabaseError as e:
        # corrupt derived index: quarantine (never delete), rebuild, retry once
        with core.lock("index", timeout=120):
            _quarantine_db(str(e))
        reindex(None, quiet=True)
        rows = _query(slug, match, limit)
    return _rerank(rows, query, slug, limit)


def db_health():
    if not DB.exists():
        return "MISSING_REBUILDABLE"
    try:
        con = sqlite3.connect(str(DB), timeout=30)
        ok = con.execute("PRAGMA integrity_check").fetchone()[0]
        con.close()
        return "OK" if ok == "ok" else f"CORRUPT:{ok}"
    except Exception as e:
        return f"UNREADABLE:{e}"
