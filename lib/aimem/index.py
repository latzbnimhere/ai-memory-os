"""SQLite FTS5 derived index with local deterministic re-ranking.

Ranking signals (all local, no embeddings):
  - bm25 relevance (FTS5)
  - kind authority weight (CURRENT/NEXT > checkpoint > decision/provenance > summaries > knowledge > events > cold journals)
  - recency (mtime decay over 30 days)
  - current-checkpoint authority (+)
  - verified provenance (+)
  - exact phrase match (+)
  - checkpoint/phase label match (+)
Project scope is enforced by the WHERE clause.
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


def _open():
    con = sqlite3.connect(str(DB), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def db_connect():
    core.ensure_root()
    try:
        con = _open()
    except sqlite3.DatabaseError as e:
        _quarantine_db(str(e))
        con = _open()
    con.execute("""
        CREATE TABLE IF NOT EXISTS chunks_meta(
            id INTEGER PRIMARY KEY, project TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
            chunk_no INTEGER NOT NULL, sha256 TEXT NOT NULL, mtime REAL NOT NULL)
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
    # Upgrade path from V3 schema (no mtime column in fts)
    cols = [r[1] for r in con.execute("PRAGMA table_info(chunks_fts)").fetchall()]
    if "mtime" not in cols:
        con.execute("DROP TABLE chunks_fts")
        con.execute("DELETE FROM chunks_meta")
        con.execute("""
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                body, project UNINDEXED, path UNINDEXED, kind UNINDEXED, chunk_no UNINDEXED, mtime UNINDEXED,
                tokenize='unicode61')
        """)
        con.commit()
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
    roots = [(slug, project_dir(slug))] if slug else [(s, project_dir(s)) for s in registry()["projects"]]
    for s, root in roots:
        if not root.exists():
            continue
        for f in root.rglob("*"):
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
            try:
                if f.stat().st_size > cfg["max_index_file_bytes"]:
                    continue
            except OSError:
                continue
            yield s, f


def reindex(slug=None, quiet=False):
    core.ensure_root()
    if slug and not project_dir(slug).exists():
        core.die(f"Unknown project: {slug}")
    with core.lock("index", timeout=120):
        con = db_connect()
        if slug:
            con.execute("DELETE FROM chunks_meta WHERE project=?", (slug,))
            con.execute("DELETE FROM chunks_fts WHERE project=?", (slug,))
        else:
            con.execute("DELETE FROM chunks_meta")
            con.execute("DELETE FROM chunks_fts")
        files = chunks = 0
        for s, f in iter_index_files(slug):
            try:
                raw = f.read_text(errors="ignore")
                rel = f.relative_to(ROOT)
                sha = sha256_file(f)
                mtime = f.stat().st_mtime
            except Exception:
                continue
            files += 1
            kind = classify_path(rel)
            for i, body in enumerate(split_chunks(raw)):
                con.execute("INSERT INTO chunks_meta(project,path,kind,chunk_no,sha256,mtime) VALUES(?,?,?,?,?,?)",
                            (s, str(rel), kind, i, sha, mtime))
                con.execute("INSERT INTO chunks_fts(body,project,path,kind,chunk_no,mtime) VALUES(?,?,?,?,?,?)",
                            (body, s, str(rel), kind, i, mtime))
                chunks += 1
        con.commit()
        con.close()
    if not quiet:
        print(f"REINDEX=PASS files={files} chunks={chunks} db={DB}")
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
    scored.sort(key=lambda x: -x["score"])
    return scored[:limit]


def fts_search(slug, query, limit=12):
    terms = search_terms(query)
    if not terms:
        return []
    if DB.exists() and db_health() != "OK":
        _quarantine_db(db_health())
    if not DB.exists():
        reindex(None, quiet=True)
    con = db_connect()
    match = " OR ".join('"' + t.replace('"', '') + '"' for t in terms)
    try:
        rows = con.execute("""
            SELECT path, kind, chunk_no, snippet(chunks_fts,0,'[[',']]',' … ',42) AS snip,
                   bm25(chunks_fts) AS rank, body, mtime
            FROM chunks_fts WHERE chunks_fts MATCH ? AND project=? ORDER BY rank LIMIT ?
        """, (match, slug, int(limit) * 6)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    con.close()
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
