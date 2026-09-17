# Recovery protocol

Detection (`aimem recover`, `aimem health`, `aimem doctor`, `begin` warnings): a session is RECOVERY_REQUIRED when it is
OPEN, its lease is STALE (>= 2h without heartbeat) or ABANDONED (>= 24h), and either the repo changed, CURRENT.md changed,
or steps were logged, with no finish checkpoint. Active long commands are protected: log steps or run `aimem heartbeat`.

Packet (`aimem recover --session <id>`, written to `.generated/RECOVERY-<id>.md`): session/task, last meaningful step,
starting vs current repo state, changed files (git diff + working tree), last test state, last verified result, unresolved
errors after the last PASS, PASS steps that must not be repeated, unresolved transactions. Nothing is rerun automatically.

Resolution: verify physical state; `aimem session close <slug> --session <id> --result ABANDONED --note "..."`; continue in a new
session. Interrupted canonical writes: `aimem txn <slug>` (inspect) then `aimem txn <slug> --repair` (PREPARED with complete
staging or COMMITTING -> roll forward; PREPARED with missing staging -> roll back; anything else -> manual review).
