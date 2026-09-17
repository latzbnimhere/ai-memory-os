# AI MEMORY OS V4 — AGENT PROTOCOL

Root: `~/AI-Memory` (canonical, local-only, vendor-neutral). Version: 4.1.0. No API, no cloud, no telemetry.

## 1. Detect and begin
```bash
SLUG="$(~/AI-Memory/bin/aimem-detect "$PWD" 2>/dev/null || true)"
~/AI-Memory/bin/aimem begin "$SLUG" --agent <claude|codex|gemini|cursor|other> --task "<task>" --tokens 6000
```
Output lines: `SESSION_ID=`, `CONTEXT=`, `APPROX_TOKENS=`, `MEMORY_VERSION=`, `RECONCILIATION=` (+`RECONCILE_CONTEXT=` when
not MEMORY_MATCH), optional `WARN=` lines. Capture the exact SESSION_ID. Read CONTEXT first. Never load `~/AI-Memory` recursively.

Reconciliation statuses: MEMORY_MATCH, PHYSICAL_AHEAD, MEMORY_AHEAD_OR_UNVERIFIED, DIVERGED, RUNTIME_VERIFICATION_REQUIRED,
plus flags DIRTY_REPO / CURRENT_MD_HEAD_MISMATCH. Anything but MEMORY_MATCH means: verify physical state before trusting memory.
Freshly verified physical state overrides stale memory. CURRENT.md is never rewritten from git alone.

## 2. Log meaningful steps (explicit binding)
```bash
~/AI-Memory/bin/aimem-step --project "$SLUG" --session "$SESSION_ID" --agent <agent> \
  --kind <plan|read|command|write|test|decision|error|result|checkpoint|other> --summary "<compact fact>" [--result R] [--file F] [--command C]
```
A meaningful step is a batch of related reads, a command, a write, a test, a decision, an error/stop, or a result. Each step
refreshes the session heartbeat (lease). Long-running commands: `aimem heartbeat "$SLUG" --session "$SESSION_ID"`.
Never log hidden reasoning, secrets, or huge output (values are redacted and truncated anyway).

## 3. Sessions and leases
Lease states: ACTIVE (heartbeat < 2h), STALE (< 24h), ABANDONED (>= 24h), CLOSED. Conservative on purpose.
If more than one session is OPEN, every mutation requires `--session`; otherwise it fails closed (exit 6).

## 4. Closeout
1. Update `~/AI-Memory/projects/$SLUG/CURRENT.md` and `NEXT.md` compactly (or use `aimem write-current` for a transactional, version-checked write).
2. `aimem note "$SLUG" --kind decision|error --text "..." --session "$SESSION_ID"` for durable facts.
3. `aimem finish "$SLUG" --session "$SESSION_ID" --result <PASS|STOP|PARTIAL|...> --label "<checkpoint>" [--summary "..."]`
   - `--allow-unchanged` only for a genuinely read-only phase; `--no-advance-current` for administrative checkpoints.
   - Exit 3 = VERSION_CONFLICT / CANONICAL_STATE_ADVANCED_BY_ANOTHER_SESSION: another agent advanced memory. Re-read CURRENT/NEXT,
     merge, then retry with `--expect-version <shown>`. Never overwrite blindly.
Truthful results only. Never claim PASS without verification.

## 5. Recovery
`aimem recover [slug] [--session id]` is read-only: it lists OPEN sessions whose lease is STALE/ABANDONED with changes and writes a
bounded recovery packet (`.generated/RECOVERY-<id>.md`). Nothing is rerun automatically. After verifying, close the dead session:
`aimem session close <slug> --session <id> --result ABANDONED --note "<why>"` and continue in a new session.

## 6. Authority order
1. explicit current-session owner instruction; 2. freshly verified physical repo/runtime state; 3. CURRENT.md / NEXT.md;
4. latest immutable checkpoint; 5. provenance facts, decisions/events, retrieved history.

## 7. Provenance
`aimem fact set <slug> --key <k> --value <v> --source-type <OWNER_INSTRUCTION|PHYSICAL_GIT|PHYSICAL_RUNTIME|DATABASE_QUERY|LOCAL_FILE|CHECKPOINT|CLAUDE_SESSION|CODEX_SESSION|CHATGPT_HANDOFF|DERIVED|UNVERIFIED> --status <VERIFIED|REPORTED|INFERRED|UNCONFIRMED> --session $SESSION_ID`
`aimem fact list <slug>` shows the latest fact per key; PHYSICAL_GIT facts are shown STALE when the live repo differs.

## 8. Secrets
Never store passwords, API keys, private keys, cookies, tokens, recovery codes. `write-current` and `chat-import` reject secret patterns.

## 9. Optional global project handoffs (owner-authorized R1)
When `registry/handoffs.json` exists, `aimem finish` / accepted checkpoint persistence
runs the bounded handoff bridge after local memory is safe. Core memory remains local;
only explicit navigation metadata is published through the pinned Drive desktop folder.
Before closeout, prepare the concise structured state described in
`~/AI-Memory/docs/HANDOFF_BRIDGE_R1.md`, bound to the exact CURRENT/NEXT SHA256 and
`checkpoint: NEXT_ACCEPTED_CHECKPOINT`; store it using
`aimem handoff --project <mapped-id-or-slug> --set-state <local-json-file>`.
Missing/stale authority becomes NEEDS_RECONCILIATION; never invent a project PASS.
Observe HANDOFF_SYNC output. On FAILED preserve local success and use
`aimem handoff --retry-pending`; on REFUSED_SECRET fix the local source first.
Drive files are CONTEXT/NAVIGATION ONLY, never mutation or irreversible-phase authority.
Do not sync raw reports, credentials, customer documents or private recovery archives.
The separately required SampleProject local handoff procedure remains in force.
