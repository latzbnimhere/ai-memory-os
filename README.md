# AI Memory OS V4.1 (4.1.0)

Local, vendor-neutral continuation memory for AI coding and operations agents.
Huge durable history, minimum relevant context. No API, no cloud, no telemetry, $0.

- Install root (runtime): `~/AI-Memory` — `bin/` launchers, `lib/aimem/` engine, `projects/<slug>/` canonical memory.
- Development repo: this directory. Private runtime state is kept outside the source tree.
- Python 3.9+ standard library only (SQLite FTS5, fcntl locks, http.server bound to 127.0.0.1).

## Quick start (agents)
```bash
SLUG="$(~/AI-Memory/bin/aimem-detect "$PWD")"
~/AI-Memory/bin/aimem begin "$SLUG" --agent claude --task "..." --tokens 6000     # SESSION_ID=, CONTEXT=, RECONCILIATION=
~/AI-Memory/bin/aimem-step --project "$SLUG" --session "$SESSION_ID" --kind write --summary "..." --result PASS
~/AI-Memory/bin/aimem finish "$SLUG" --session "$SESSION_ID" --result PASS --label "checkpoint"
```

## Docs
[Architecture](docs/ARCHITECTURE.md) · [Commands](docs/COMMANDS.md) · [Agent protocol](docs/AGENT_PROTOCOL.md) ·
[Recovery](docs/RECOVERY.md) · [Backup/restore](docs/BACKUP_RESTORE.md) · [ChatGPT bridge](docs/CHATGPT_BRIDGE.md) ·
[Dashboard](docs/DASHBOARD.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Migration from V3.1.1](docs/MIGRATION_V3_1_1_TO_V4.md) ·
[Rollback](docs/ROLLBACK.md) · [Security / no-API policy](docs/SECURITY_NO_API.md)

## Tests
```bash
python3 -m unittest tests.test_v4          # unit tests
python3 bin/aimem selftest --full          # isolated end-to-end + stress + crash recovery (temp root only)
python3 tools/promote.py --rehearse --root /path/to/AI-Memory
```

## Open-source safety model

AI Memory OS separates the reusable engine from private project memory.

The repository contains code, documentation, tests, and generic examples. Real project state belongs in the user's local runtime directory and should not be committed.

Key safety properties include:

- local-first, vendor-neutral operation
- bounded context generation
- explicit session binding
- fail-closed ambiguity handling
- secret detection and redaction
- transactional canonical writes
- isolated self-tests
- explicit-root live promotion

See `SECURITY.md` and `docs/PUBLIC_SAFETY.md` before using live promotion features.
