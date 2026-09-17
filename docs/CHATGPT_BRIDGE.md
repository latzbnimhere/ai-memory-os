# ChatGPT bridge (no API)

Export: `aimem chat-export <slug> --query "<task>" --tokens 6000 [--output F]` writes an `AI_MEMORY_CHATGPT_CONTEXT_V1` file:
project identity, memory version, checkpoint, physical head at export, compact CURRENT/NEXT, ranked relevant history,
and a reader contract stating that ChatGPT has no Mac filesystem access and must not invent physical state. Paste it into the chat.

Import: `aimem chat-import <slug> <packet.md> [--dry-run] [--note] [--session]` accepts `AI_MEMORY_AGENT_PACKET_V1`
(the block ChatGPT emits between `AI_MEMORY_AGENT_PACKET BEGIN/END`). It: validates the format (version, slug), scans for secret
patterns, extracts claimed HEAD hashes/branches, compares them with CURRENT.md and fresh git state, stores the packet verbatim
(object store + `knowledge/chat-imports/`), hashes it, records EVENTS/ARTIFACTS/PROVENANCE (claims = REPORTED, never VERIFIED),
and writes a reconciliation report. CURRENT.md is never modified. Conflicts (slug mismatch, live HEAD absent from packet,
format errors, secrets) exit 3 CONFLICTS_REQUIRE_RESOLUTION. `--dry-run` writes only a report under `.generated/`.
`aimem import-handoff` is kept as an alias for non-dry-run import.
