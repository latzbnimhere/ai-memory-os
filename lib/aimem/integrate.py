"""Managed instruction blocks for Claude / Codex / other filesystem-capable agents.

Global files: ~/.claude/CLAUDE.md, ~/.codex/AGENTS.md. Only content between aimem markers
is managed; every other user instruction is preserved verbatim. A backup copy is written
before the first V4 edit of each file.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from . import core
from .core import ROOT, atomic_write, stamp

V4_BEGIN = "<!-- AI-MEMORY-OS-V4-BEGIN -->"
V4_END = "<!-- AI-MEMORY-OS-V4-END -->"
# Legacy V2/V3 managed blocks (all aimem-owned) are superseded by the single V4 block.
LEGACY_BLOCKS = [
    ("<!-- AI-MEMORY-OS-AUTOPILOT-BEGIN -->", "<!-- AI-MEMORY-OS-AUTOPILOT-END -->"),
    ("<!-- AI-MEMORY-OS-CONTINUOUS-JOURNAL-BEGIN -->", "<!-- AI-MEMORY-OS-CONTINUOUS-JOURNAL-END -->"),
    ("<!-- AI-MEMORY-SESSION-BINDING-V3.1-BEGIN -->", "<!-- AI-MEMORY-SESSION-BINDING-V3.1-END -->"),
    ("<!-- AIMEM-MANAGED-BEGIN -->", "<!-- AIMEM-MANAGED-END -->"),
]


def block(agent):
    root = str(ROOT)
    return f"""{V4_BEGIN}
## AI Memory OS V4 (local, vendor-neutral continuation memory)

This Mac keeps authoritative project continuation memory in `{root}` (AI Memory OS {core.VERSION}).
Protocol: `{root}/AI_MEMORY_AGENT_PROTOCOL_V4.md`. Local only: no API, no cloud, no telemetry.

For meaningful work inside a registered project:
1. Detect: `SLUG="$({root}/bin/aimem-detect "$PWD" 2>/dev/null || true)"`; if empty, memory is not required.
2. Begin: `{root}/bin/aimem begin "$SLUG" --agent {agent} --task "<current task>" --tokens 4500`
   Use `--mode deep --tokens 6000` only when broad historical/recovery context is genuinely required.
   Capture the exact `SESSION_ID=`. Read the returned `CONTEXT=` file first; if `RECONCILIATION=` is not
   `MEMORY_MATCH`, read `RECONCILE_CONTEXT=` and verify physical state before trusting memory.
3. Log meaningful operational steps (reads batch, command, write, test, decision, error, result) with explicit binding:
   `{root}/bin/aimem-step --project "$SLUG" --session "<SESSION_ID>" --agent {agent} --kind <kind> --summary "<compact fact>" [--result R] [--file F]`
   Not every microscopic tool call. Never hidden reasoning, never secrets.
For long-running work that may exceed 30 minutes without an `aimem-step`, refresh the exact lease before
   and after the wait:
   `{root}/bin/aimem heartbeat "$SLUG" --session "<SESSION_ID>" --note "long-running work still active"`

4. At meaningful closeout: update `{root}/projects/$SLUG/CURRENT.md` and `NEXT.md` compactly, record durable
   decisions/errors with `aimem note`, then finish truthfully:
   `{root}/bin/aimem finish "$SLUG" --session "<SESSION_ID>" --result <PASS|STOP|PARTIAL|...> --label "<checkpoint>"`
   Use `--allow-unchanged` only for a genuinely read-only phase; `--no-advance-current` for administrative checkpoints.
   If finish reports a VERSION_CONFLICT, another agent advanced memory: re-read CURRENT/NEXT, merge, retry with `--expect-version`.

Rules: never guess a session id when several are open (fail closed); freshly verified physical state overrides stale memory;
do not rewrite CURRENT.md from git alone; do not load all of `{root}` recursively; keep large outputs in artifacts/knowledge.
Recovery: `aimem recover "$SLUG"` (read-only) when a previous session was interrupted. Health: `aimem health`.
{V4_END}"""


def strip_legacy(text):
    for b, e in LEGACY_BLOCKS:
        text = re.sub(re.escape(b) + r".*?" + re.escape(e) + r"\n?", "", text, flags=re.S)
    return text


def patch_file(path: Path, agent, dry_run=False):
    path = Path(path).expanduser()
    new_block = block(agent)
    if not path.exists():
        result = "CREATED"
        new_text = f"# AI Agent Instructions\n\n{new_block}\n"
    else:
        text = path.read_text(errors="ignore")
        if V4_BEGIN in text and V4_END in text:
            new_text = re.sub(re.escape(V4_BEGIN) + r".*?" + re.escape(V4_END), new_block.strip(), text, flags=re.S)
            result = "UPDATED_MANAGED_BLOCK"
        else:
            stripped = strip_legacy(text).rstrip()
            new_text = (stripped + "\n\n" if stripped else "") + new_block + "\n"
            result = "REPLACED_LEGACY_BLOCKS" if stripped != text.rstrip() else "APPENDED"
        if new_text == text:
            return "UNCHANGED"
        if not dry_run:
            bak = path.with_name(path.name + f".pre-aimem-v4-{stamp()}.bak")
            shutil.copy2(path, bak)
            result += f" BACKUP={bak.name}"
    if not dry_run:
        atomic_write(path, new_text)
    return result


def global_targets():
    return [(Path.home() / ".claude" / "CLAUDE.md", "claude"), (Path.home() / ".codex" / "AGENTS.md", "codex")]


def verify_file(path: Path, agent):
    path = Path(path).expanduser()
    if not path.exists():
        return False
    t = path.read_text(errors="ignore")
    return V4_BEGIN in t and V4_END in t and f"--agent {agent}" in t and "aimem-step" in t and "SESSION_ID" in t
