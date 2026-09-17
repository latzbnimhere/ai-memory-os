# Installation

AI Memory OS keeps the reusable source repository separate from the private runtime root.

## Requirements

- Python 3.9 or newer.
- A POSIX-style environment with `fcntl` support for the core locking model.
- The current automated install path is validated on macOS. LaunchAgent integration is macOS-specific.

No API key, database service, cloud account, package manager, or third-party Python dependency is required.

## Fresh install

A fresh install is allowed only when the target runtime root does not already exist.

```bash
git clone <this-repository-url> ai-memory-os-src
cd ai-memory-os-src
python3 tools/install.py --root "$HOME/AI-Memory"
```

On success the installer prints `INSTALL=PASS`.

The installer:

1. Requires an explicit `--root`.
2. Refuses an existing target, even if it is empty.
3. Refuses a runtime root inside the source repository.
4. Copies the runtime launchers, Python engine, protocol files, documentation, and version file.
5. Initializes a new empty AI Memory root.
6. Runs `doctor --deep --no-repo` against that new root.
7. Removes the newly created root if any install or verification step fails.
8. Does not modify global agent files, LaunchAgents, unrelated repositories, or any existing AI Memory installation.

## Verify

```bash
AI_MEMORY_ROOT="$HOME/AI-Memory" ~/AI-Memory/bin/aimem version
AI_MEMORY_ROOT="$HOME/AI-Memory" ~/AI-Memory/bin/aimem doctor --deep --no-repo
```

## Register the first project

Run this from a project repository:

```bash
cd /path/to/project
~/AI-Memory/bin/aimem register example-project --name "Example Project" --repo "$PWD"
~/AI-Memory/bin/aimem status
```

The slug and display name are examples. Do not store credentials, customer data, or secrets in project memory.

## Agent integration

Global instruction integration is intentionally separate from installation. Review the generated instructions and the agent protocol before enabling it.

See:

- `docs/AGENT_PROTOCOL.md`
- `share/OTHER_AI_AGENT_INSTRUCTIONS.md`
- `docs/SECURITY_NO_API.md`

## Existing installations

Do not use the fresh installer over an existing runtime root. Use the controlled rehearsal first:

```bash
python3 tools/promote.py --rehearse --root /path/to/AI-Memory
```

Live promotion requires the explicit target path a second time:

```bash
python3 tools/promote.py \
  --live \
  --root /path/to/AI-Memory \
  --confirm-live-root /path/to/AI-Memory
```

A mismatch fails closed.

## Public-distribution audit

Before publishing a source tree, run:

```bash
python3 tools/public_audit.py
```

To check additional private names locally without committing them to the repository:

```bash
python3 tools/public_audit.py --deny-term "private-name" --deny-term "internal-project"
```

The deny terms are supplied only at runtime and are not stored by the audit tool.
