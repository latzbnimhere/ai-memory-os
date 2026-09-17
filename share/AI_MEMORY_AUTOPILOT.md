# AI MEMORY AUTOPILOT (V4)

This machine uses `~/AI-Memory` (AI Memory OS 4.1.0) as vendor-neutral project continuation memory.
Full protocol: `~/AI-Memory/AI_MEMORY_AGENT_PROTOCOL_V4.md`.

Activate for meaningful work when `~/AI-Memory/bin/aimem-detect "$PWD"` prints a slug:
1. `aimem begin "$SLUG" --agent <agent> --task "<task>" --tokens 6000`; capture SESSION_ID; read CONTEXT first;
   act on RECONCILIATION (verify physical state unless MEMORY_MATCH).
2. Log meaningful steps with `aimem-step --project "$SLUG" --session "<SESSION_ID>" ...`.
3. Closeout: update CURRENT.md/NEXT.md, `aimem note`, `aimem finish "$SLUG" --session "<SESSION_ID>" --result <TRUTHFUL> --label "<checkpoint>"`.
Never load all of `~/AI-Memory`. Never store secrets. Never claim PASS without verification. Fresh physical state beats stale memory.
