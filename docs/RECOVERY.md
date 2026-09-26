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
While a transaction is pending, finish/checkpoint/write-current fail closed with exit 4 (`UNRESOLVED_TRANSACTIONS`).
`MANUAL_TARGET_CHANGED`: a target was rewritten after the interruption (it matches neither its pre-image nor the staged
content). Repair will not apply the stale staged copy. Compare the staged file (`.<name>.<txid>.staged`) with the live
file, keep the correct content, then delete the staged file and the record in `projects/<slug>/.txn/`.

Torn journal lines (a crash mid-append): `aimem doctor` reports `invalid JSONL <file>:<line>`; later appends start on a
new line, so only that line is affected. `aimem doctor --repair` moves unparseable lines out of the journal in place
(under the append lock) and preserves the complete original as `cold/quarantine/<file>.<stamp>.orig` plus the bad lines
as `.bad`.

Incomplete checkpoints (a directory under `checkpoints/` without `meta.json`, or a V4 checkpoint without `txn` left by a
refused finish in an engine older than 4.2) are reported by `doctor --deep`, ignored by index and context, and never
deleted automatically. Archive them manually after review.
