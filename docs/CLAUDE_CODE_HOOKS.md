# Claude Code hooks (automatic protocol)

With hooks installed, Claude Code drives the agent protocol itself: session begin, bounded context, step
logging, heartbeat and close happen without the agent remembering a single command. The agent still owns
the closeout (`CURRENT.md`, `NEXT.md`, `aimem finish`), because only it can state a truthful result.

## Install (opt-in, reversible)

```bash
~/AI-Memory/bin/aimem hooks install      # merge 7 hooks into ~/.claude/settings.json (backup written first)
~/AI-Memory/bin/aimem hooks status       # INSTALLED / PARTIAL / NOT_INSTALLED
~/AI-Memory/bin/aimem hooks uninstall    # remove only the aimem entries
~/AI-Memory/bin/aimem hooks show         # print the JSON block without writing anything
```

`--target PATH` manages a project-local `.claude/settings.json` instead of the global file; `--dry-run` reports
without writing. Like `integrate-global`, this is separate from `tools/install.py` and never runs implicitly.
Only entries whose command ends with `aimem hooks run` are managed; every other hook and setting is preserved
byte-for-byte in meaning (the file is rewritten as indented JSON). An invalid settings file is refused untouched.

Cloud Claude Code sessions do not read `~/.claude/settings.json`; use a project-local target there.

## What each event does

| Claude Code event | aimem action |
|---|---|
| `SessionStart` (startup, resume, clear, compact, fork) | detect the project from `cwd`; `begin` a session bound to the Claude Code session id, or reuse the open one on resume/compact; print the bounded context pack plus an `AI MEMORY OS HOOKS` footer into the agent's context |
| `UserPromptSubmit` | the first prompt becomes the session task; every prompt is logged as a `plan` step (`"hook_log_prompts": false` in `config.json` keeps only the heartbeat) |
| `PostToolUse` for `Edit`, `MultiEdit`, `Write`, `NotebookEdit` | `write` step with the file path |
| `PostToolUse` for `Bash` | `command` step, or `test` when the command looks like a test runner; the command text is stored redacted and truncated |
| `PostToolUseFailure` (same tools) | `error` step, also appended to `EVENTS.jsonl` |
| `Stop` | heartbeat (keeps the lease ACTIVE) |
| `PreCompact` | `other` step marking the compaction; the following `SessionStart` re-injects a fresh pack |
| `SessionEnd` | close the session without a checkpoint: `UNFINISHED` when meaningful steps exist, `EMPTY` otherwise; the binding is removed |

Reads (`Read`, `Glob`, `Grep`) are never logged: the matcher does not invoke the hook for them.

## The footer the agent sees

```
AIMEM_HOOKS=ACTIVE
AIMEM_PROJECT=<slug>
AIMEM_SESSION_ID=<id>
AIMEM_ROOT=~/AI-Memory
RECONCILIATION=<status>            [RECONCILE_CONTEXT=<path> when not MEMORY_MATCH]
[PREVIOUS_SESSION_UNFINISHED=<id>] when the last session ended without a checkpoint
[FULL_CONTEXT=<path>]              when the pack had to be shortened (see below)
```

followed by the exact `aimem-step`, `aimem note` and `aimem finish` commands for this session. The managed
`CLAUDE.md` block (`aimem integrate-global`) tells the agent to skip its manual begin/step when this footer is present.

## Guarantees

- Fail open. The runner never exits non-zero and never blocks a tool call; any engine problem is reported on
  stderr as `AIMEM_HOOK=SKIPPED ...` (visible with `claude --debug`). `AIMEM_HOOKS_DISABLE=1` turns the hooks
  off for one shell.
- Silent outside registered projects. Register a repository and hooks start on the next Claude Code session, or
  late-bind on the next logged tool call of the current one.
- Explicit binding. Every step carries `session_binding: hook` and the exact session id. A second Claude Code
  session in the same repository gets its own aimem session; manual commands must then pass `--session`.
- Bounded output. Claude Code caps plain hook stdout at 10,000 characters. When the pack plus footer would exceed
  that, a smaller pack is built for stdout and `FULL_CONTEXT=` points at the full file under `.generated/`.
- Same secret redaction and truncation as `aimem-step`. Nothing leaves the machine.

## Closeout with hooks

```bash
~/AI-Memory/bin/aimem finish <slug> --session <AIMEM_SESSION_ID> --result <PASS|STOP|PARTIAL> --label "<checkpoint>"
```

A finished session is left alone by later `Stop`/`SessionEnd` events. If the agent never finishes, the next
`SessionStart` in that project shows `PREVIOUS_SESSION_UNFINISHED=<id>` with the path to its step log, so the
next agent knows the last state was not checkpointed. `aimem recover` remains the path for crashed sessions.

## Configuration

| `config.json` key | Default | Meaning |
|---|---|---|
| `hook_context_tokens` | `null` (= `default_context_tokens`) | token budget requested for the SessionStart pack |
| `hook_log_prompts` | `true` | log each user prompt as a `plan` step and use the first one as the session task |

Bindings live in `.run/hooks/claude/<claude-session-id>.json` and are disposable runtime state.
