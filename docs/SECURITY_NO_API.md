# Security and no-API policy
- Runtime imports: standard library only; no `urllib`/`requests`/sockets outside the loopback dashboard server and the selftest client.
- No API keys, no remote embeddings, no cloud/database/telemetry; SQLite FTS5 + deterministic ranking only. Additional cost: $0.
- Dashboard binds 127.0.0.1 only (hard-coded).
- Secret scanning: OpenAI/Anthropic/GitHub/AWS/Slack/Google keys, private keys, JWTs, bearer/cookie headers, password assignments.
  Step summaries, notes, facts and tasks are redacted before storage; `write-current` and `chat-import` reject secret patterns;
  `doctor --deep` scans CURRENT/NEXT/DECISIONS/PROVENANCE.
- Never store passwords, API keys, private keys, cookies, tokens, recovery codes or raw credentials.
- Backups are plain tar.gz on local disk (`~/AI-Memory-Backups`); protect that directory like the memory itself.
