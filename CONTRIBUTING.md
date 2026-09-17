# Contributing to AI Memory OS

Contributions are welcome.

## Core principles

Changes should preserve:

1. Local-first operation.
2. Vendor-neutral AI agent support.
3. Bounded context instead of recursively loading complete archives.
4. Fresh verified physical state overriding stale memory.
5. Fail-closed behavior when session ownership or authority is ambiguous.
6. No secrets in memory.
7. Tests that never touch a user's real AI Memory installation.

## Testing

Run:

    python3 -m unittest discover -s tests -v

Then:

    python3 bin/aimem selftest --full

Development and CI should use a temporary AI_MEMORY_ROOT.

## Pull requests

Include:

- what changed
- why it changed
- tests run
- safety implications
- migration or rollback considerations when relevant

Never submit real project memory, credentials, customer data, production paths, or private artifacts.
