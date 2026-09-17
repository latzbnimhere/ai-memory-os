# AI MEMORY SESSION BINDING — V3.1

When more than one AI agent works on the same project, never identify a session by
"newest open session".

After `aimem begin`, capture the exact `SESSION_ID=<id>` and pass it explicitly to:

```bash
~/AI-Memory/bin/aimem-step --session "<SESSION_ID>" ...
aimem finish <slug> --session "<SESSION_ID>" ...
```

If more than one session is open and no id is supplied, memory mutation must fail
closed instead of guessing.

For a memory-only/read-only administrative checkpoint that should not replace the
project's semantic current checkpoint, use `--no-advance-current`.

Store operational facts only. Never store hidden chain-of-thought or secrets.
Fresh verified physical state overrides stale memory.

## V4 addendum
V4 keeps this rule unchanged and adds leases/heartbeats, compare-and-swap on finish (`--expect-version`), and exit codes:
0 ok, 1 warn, 2 error, 3 conflict, 4 recovery required, 5 lock timeout, 6 ambiguous session.
