# Agent protocol
The installed authority is `~/AI-Memory/AI_MEMORY_AGENT_PROTOCOL_V4.md` (source: `share/AI_MEMORY_AGENT_PROTOCOL_V4.md`).
Global instruction files carry a single managed block `<!-- AI-MEMORY-OS-V4-BEGIN/END -->` (installed by `aimem integrate-global`)
for `~/.claude/CLAUDE.md` (agent claude) and `~/.codex/AGENTS.md` (agent codex); other agents use `OTHER_AI_AGENT_INSTRUCTIONS.md`.
Required agent behaviour: detect -> begin -> capture SESSION_ID -> read bounded context -> log meaningful steps with explicit
binding -> update CURRENT/NEXT at closeout -> finish truthfully. Not every microscopic tool call; never hidden reasoning; never secrets.
