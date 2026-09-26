# Troubleshooting
- `AMBIGUOUS_OPEN_SESSIONS` (exit 6): pass `--session <id>`; list with `aimem sessions <slug> --open`.
- `VERSION_CONFLICT` / `CANONICAL_STATE_ADVANCED_BY_ANOTHER_SESSION` (exit 3): re-read CURRENT/NEXT, merge, retry with `--expect-version N`.
- `LOCK_TIMEOUT` (exit 5): another agent is writing the same project (`last_holder` names its pid); retry. Locks die with
  their process, so a lock file on disk is never "stale"; do not delete `.locks/`. Raise `lock_timeout_s` if needed.
- `RECOVERY_REQUIRED` (exit 4): `aimem recover <slug>`, read the packet, close the dead session, continue in a new one.
- `unresolved transaction` / `UNRESOLVED_TRANSACTIONS` (exit 4 on finish/checkpoint/write-current): `aimem txn <slug>` then
  `aimem txn <slug> --repair`. `MANUAL_TARGET_CHANGED`: see RECOVERY.md.
- `SECRET_PATTERN_REJECTED` at finish/checkpoint: remove the credential from CURRENT.md/NEXT.md (checkpoints are immutable);
  `--allow-secret-pattern` only for a verified false positive.
- `UNSAFE_SLUG` / `UNSAFE_SESSION` / `Unknown project`: slugs and session ids are single path components; check the spelling
  (`aimem status`, `aimem sessions <slug>`).
- `aimem-detect` exit 6 / `AMBIGUOUS_PROJECT`: one repo is registered under several slugs; pass `--project` or fix the registry.
- `invalid JSONL <file>:<n>`: a torn write; `aimem doctor --repair` quarantines it losslessly (see RECOVERY.md).
- `stale temp file` / `incomplete checkpoint`: leftovers of an interrupted writer; never canonical. See RECOVERY.md.
- `memory.db BROKEN CORRUPT:...` or `memory.db BROKEN UNREADABLE:...`: the derived index is damaged. Both classes mean the
  same thing: SQLite reports identical damage either as integrity-check rows (CORRUPT) or as an error while checking
  (UNREADABLE: "malformed", "not a database", "string or blob too big", ...), depending on the SQLite build. begin/search
  never crash on it (search rebuilds when a query hits the damage); `aimem doctor --repair` quarantines the file as
  `memory.db.corrupt-*` (never deleted) and rebuilds it from canonical files; canonical files are not touched.
- `memory.db UNAVAILABLE:database is locked` (warning): the index is busy, not damaged; it is never quarantined. Re-run.
- `WARN: INDEX_REFRESH_DEFERRED` after finish/checkpoint: the canonical write committed; only the derived index refresh
  was skipped (busy or unusable). The next search refreshes incrementally; `aimem doctor` reports real damage.
- `CURRENT.md did not change`: update it, or use `--allow-unchanged` for a read-only phase.
- `RECURSIVE_INDEX_RISK`: never register `~/AI-Memory` or a parent of it.
- Context too large: keep CURRENT.md compact; move detail to knowledge/ or checkpoints; `aimem health` warns above ~9k tokens.
- Dashboard will not start: see `~/AI-Memory/.run/dashboard.log`; port busy -> `--port`.
- Rollback to V3.1.1: see ROLLBACK.md.
