# AI Memory OS V4.1 (4.1.0)

Local, vendor-neutral continuation memory for AI coding and operations agents.
Huge durable history, minimum relevant context. No API, no cloud, no telemetry, $0.

- Install root (runtime): `~/AI-Memory` — launchers, engine, and private runtime state.
- Source repository: keep it separate from the runtime root.
- Python 3.9+ standard library only (SQLite FTS5, POSIX `fcntl` locks, local dashboard bound to `127.0.0.1`).

## Fresh install

Clone this repository into a source directory, then install into a new runtime root:

```bash
git clone <this-repository-url> ai-memory-os-src
cd ai-memory-os-src
python3 tools/install.py --root "$HOME/AI-Memory"
```

The installer is intentionally fail-closed: the target root must not already exist. It copies only the runtime code/docs, initializes a new empty memory root, runs a deep no-repository doctor check, and rolls the new root back if installation fails. It does not modify an existing AI Memory installation, global agent instructions, project repositories, or launch services.

See [Installation](docs/INSTALL.md) for the complete first-run flow. Existing installations should use the controlled promotion/rehearsal path instead of the fresh installer.

## First project

From the project repository you want AI Memory OS to track:

```bash
cd /path/to/project
~/AI-Memory/bin/aimem register example-project --name "Example Project" --repo "$PWD"
```

Then an agent session can use:

```bash
SLUG="$(~/AI-Memory/bin/aimem-detect "$PWD")"
~/AI-Memory/bin/aimem begin "$SLUG" --agent other --task "..." --tokens 6000
~/AI-Memory/bin/aimem-step --project "$SLUG" --session "$SESSION_ID" --kind write --summary "..." --result PASS
~/AI-Memory/bin/aimem finish "$SLUG" --session "$SESSION_ID" --result PASS --label "checkpoint"
```

## Docs

[Installation](docs/INSTALL.md) · [Architecture](docs/ARCHITECTURE.md) · [Commands](docs/COMMANDS.md) · [Agent protocol](docs/AGENT_PROTOCOL.md) ·
[Recovery](docs/RECOVERY.md) · [Backup/restore](docs/BACKUP_RESTORE.md) · [ChatGPT bridge](docs/CHATGPT_BRIDGE.md) ·
[Dashboard](docs/DASHBOARD.md) · [Troubleshooting](docs/TROUBLESHOOTING.md) · [Migration from V3.1.1](docs/MIGRATION_V3_1_1_TO_V4.md) ·
[Rollback](docs/ROLLBACK.md) · [Security / no-API policy](docs/SECURITY_NO_API.md) · [Public safety](docs/PUBLIC_SAFETY.md)

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 bin/aimem selftest --full
python3 tools/public_audit.py
python3 tools/promote.py --rehearse --root /path/to/AI-Memory
```

GitHub Actions runs the public audit, unit suite, full isolated selftest, and fresh-install verification on supported Python versions.

## Open-source safety model

AI Memory OS separates reusable source code from private project memory. The repository contains code, documentation, tests, and generic examples only. Real project state belongs in the user's local runtime root and must not be committed.

Key safety properties include:

- local-first, vendor-neutral operation
- bounded context generation
- explicit session binding
- fail-closed ambiguity handling
- secret detection and redaction
- transactional canonical writes
- isolated self-tests
- fresh install that refuses pre-existing targets
- explicit-root live promotion with second-path confirmation
- tracked-tree public audit for secrets, personal absolute paths, public IPs, symlinks/submodules, and forbidden runtime paths

See `SECURITY.md` and `docs/PUBLIC_SAFETY.md` before using live promotion features.
