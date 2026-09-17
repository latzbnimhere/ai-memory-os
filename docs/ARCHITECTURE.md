# Architecture (V4)

```
~/AI-Memory/
├── bin/aimem, aimem-step, aimem-detect, aimem-sweep      thin launchers -> lib/aimem
├── lib/aimem/                                          engine (stdlib only)
│   core.py  txn.py  sessions.py  reconcile.py  provenance.py  index.py  context.py  compact.py
│   objects.py  chat.py  recover.py  health.py  dashboard.py  backup.py  doctor.py  migrate.py  integrate.py  selftest.py
├── registry/projects.json (canonical)  registry/memory.db (derived FTS5, rebuildable, quarantined if corrupt)
├── objects/sha256/ab/cd/<hash>                         content-addressed artifacts (read-only, deduplicated)
├── projects/<slug>/
│   project.json (memory_version, current_checkpoint)   CURRENT.md  NEXT.md  REPO_STATE.json
│   DECISIONS.jsonl  EVENTS.jsonl  ARTIFACTS.jsonl  PROVENANCE.jsonl        append-only
│   checkpoints/<id>/{CURRENT.md,NEXT.md,REPO_STATE.json,meta.json}         immutable
│   sessions/<id>.json (+ .steps.jsonl)                 session state, lease, heartbeat
│   cold/step-journal/<day>.jsonl  cold/physical-journal/  cold/txn-journal/  raw history (never deleted)
│   cold/summaries/{daily,phase}/ + CHECKPOINTS.md      deterministic compaction layers
│   knowledge/ (chat-imports/)  artifacts/  .generated/ (disposable packs)  .txn/ (in-flight transactions)
├── .locks/ (flock files)  .run/ (dashboard pid, sweep state)  logs/
└── AI_MEMORY_AGENT_PROTOCOL_V4.md and companion protocol files
```

## Invariants
- Canonical = files. The SQLite index and generated packs are derived and disposable.
- Every canonical multi-file update is a transaction (temp -> fsync -> validate -> atomic replace) with a `.txn` record;
  PREPARED/COMMITTING records are detectable and deterministically recoverable (`aimem txn --repair`).
- `project.json.memory_version` increments on every canonical write; finish/checkpoint/write-current support
  compare-and-swap (`--expect-version`) and fail closed on conflict (exit 3).
- Per-project exclusive write lock (`.locks/<slug>.write.lock`, bounded wait, exit 5 on timeout); per-project step lock
  for journal appends; JSONL appends are flock-protected and fsynced. Different projects never block each other.
- Sessions: explicit SESSION_ID binding (fail closed when ambiguous), lease state derived from heartbeat age.
- `begin` reconciles memory vs fresh git state and reports it; CURRENT.md is never rewritten automatically.
- Provenance facts are structured in PROVENANCE.jsonl; CURRENT.md stays human-readable.
- Retrieval: FTS5 bm25 re-ranked by kind authority, recency, current-checkpoint, verified provenance, phrase and label match.
- Compaction is rules-based (no LLM), idempotent, rebuildable, provenance-linked (source hashes), interruption-safe.
