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
- `memory.db ... UNREADABLE`/`CORRUPT`: derived index; search quarantines and rebuilds it automatically; otherwise
  `aimem doctor --repair` or `aimem reindex --full` (corrupt file is quarantined, not deleted).
- `CURRENT.md did not change`: update it, or use `--allow-unchanged` for a read-only phase.
- `RECURSIVE_INDEX_RISK`: never register `~/AI-Memory` or a parent of it.
- Context too large: keep CURRENT.md compact; move detail to knowledge/ or checkpoints; `aimem health` warns above ~9k tokens.
- Dashboard will not start: see `~/AI-Memory/.run/dashboard.log`; port busy -> `--port`.
- Rollback to V3.1.1: see ROLLBACK.md.
