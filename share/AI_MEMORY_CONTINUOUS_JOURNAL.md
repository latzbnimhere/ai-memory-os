# AI MEMORY CONTINUOUS JOURNAL (V4)

Operational facts only; no hidden chain-of-thought; no secrets.

- Start: `aimem begin` (bounded context, SESSION_ID, reconciliation).
- Every meaningful step: `~/AI-Memory/bin/aimem-step --project "$SLUG" --session "<SESSION_ID>" --agent <agent> --kind <kind> --summary "<fact>" [--result R] [--file F]`.
  Steps go to `cold/step-journal/<day>.jsonl` (raw, permanent), `sessions/<id>.steps.jsonl`, and EVENTS.jsonl for decision/error/result/checkpoint.
- Compaction (`aimem compact`) derives daily/phase/checkpoint summaries from raw steps deterministically; raw is never deleted.
- End: update CURRENT.md/NEXT.md, `aimem note`, `aimem finish ... --session "<SESSION_ID>"` with a truthful result.
- Recovery after a crash: `aimem recover`. Health: `aimem health`.
- `aimem-sweep` (launchd, 120 s) journals physical repo changes passively.
See `~/AI-Memory/AI_MEMORY_AGENT_PROTOCOL_V4.md`.
