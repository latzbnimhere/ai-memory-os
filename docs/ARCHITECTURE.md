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
- While any transaction is pending, new canonical writes (finish, checkpoint, write-current) fail closed with exit 4,
  so a later roll-forward can never overwrite newer state. Roll-forward itself is refused when a target no longer holds
  its recorded pre-image (`MANUAL_TARGET_CHANGED`). A failure after COMMITTING began keeps the record for repair.
- `project.json.memory_version` increments on every canonical write; finish/checkpoint/write-current support
  compare-and-swap (`--expect-version`) and fail closed on conflict (exit 3).
- Checkpoints are sealed entirely under the project write lock: CAS and pending-transaction checks run before the
  checkpoint directory exists, and `meta.json` (with `memory_version` and `txn`) is committed by the same transaction as
  `project.json`. A refused checkpoint leaves nothing behind. A checkpoint directory without `meta.json` is incomplete
  and ignored by index/context; doctor reports it. CURRENT/NEXT containing secret patterns are never sealed.
- Locks (`.locks/*.lock`, `fcntl.flock`, bounded wait, exit 5 on timeout; the kernel releases them when a process dies,
  so there are no stale locks; the file records the last holder for diagnostics). Locks are not re-entrant.
  Per project: `write` (canonical state), `step` (journal fan-out), `session` (session file read-modify-write:
  heartbeat, finish, close). Order: session -> write. Global: `index`. Different projects never block each other.
- JSONL appends are flock-protected and fsynced; an append after a torn (crash-truncated) line starts on a new line, so
  damage stays confined to one detectable line that `doctor --repair` quarantines losslessly.
- Slugs and session ids are validated as single safe path components everywhere; unknown projects are refused before a
  command can create anything.
- Sessions: explicit SESSION_ID binding (fail closed when ambiguous), lease state derived from heartbeat age.
- `begin` reconciles memory vs fresh git state and reports it; CURRENT.md is never rewritten automatically.
- Provenance facts are structured in PROVENANCE.jsonl; CURRENT.md stays human-readable.
- Retrieval: FTS5 bm25 re-ranked by kind authority, recency, current-checkpoint, verified provenance, phrase and label
  match; ties broken by (path, chunk). The index refreshes incrementally (per-file size/mtime, then sha256) and search
  refreshes the project first, so it sees journal entries written since the last finish.
- Compaction is rules-based (no LLM), idempotent, rebuildable, provenance-linked (source hashes), interruption-safe.

## Context packs (`begin`, `aimem context`)
Budget: `--tokens` (default 4500) is the request; smart mode targets `context_clean_target_tokens` (3600) and hot mode
3000 when memory matches the repo, while deep mode, reconciliation attention, a dirty repo or pending transactions use the
full request. Tokens are estimated as chars/3.2.

Sections are allocated by priority, not by position: NEXT, CURRENT, reconciliation (when it needs attention) and
unresolved transactions first; then owner rules, open sessions, fresh repo state, manifest, provenance, decisions,
phase summary, events, preferences. Each section first gets a capped share, leftover space is redistributed by priority,
so NEXT and CURRENT always appear. Query retrieval may use up to 38% and returns what it does not use. Journals are
rendered one compact line per record (machine-state kinds such as `repo_capture` are omitted; raw evidence stays in cold
storage); truncation drops the oldest entries. Truncated sections are marked `[TRUNCATED]`. High-confidence credential
shapes are redacted from the pack. Apart from generation timestamps, identical inputs produce identical packs.
