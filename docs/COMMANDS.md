# Commands (aimem 4.1.0)

Exit codes: 0 ok · 1 warn/fail · 2 error · 3 conflict (CAS) · 4 recovery/transactions required · 5 lock timeout · 6 ambiguous session

| Command | Purpose |
|---|---|
| `aimem init [path]` | create an empty memory root |
| `aimem version` / `aimem status` | version+lineage / per-project checkpoint, memory version, open sessions, leases, head |
| `aimem register <slug> --name N --repo P [--update]` | register a project (refuses paths overlapping the memory root) |
| `aimem begin <slug> --agent A --task T [--tokens N] [--mode hot|smart|deep]` | open session, bounded context, reconciliation |
| `aimem-step --project S --session ID --kind K --summary ... [--result] [--file] [--command]` | log a step; refreshes heartbeat |
| `aimem heartbeat <slug> --session ID [--note]` | keep lease ACTIVE during long commands |
| `aimem finish <slug> --session ID --result R [--label] [--summary] [--allow-unchanged] [--no-advance-current] [--expect-version N] [--acknowledge-newer]` | checkpoint + close (CAS) |
| `aimem checkpoint <slug> --label L --result R [--session] [--no-advance-current] [--expect-version N]` | checkpoint without closing |
| `aimem write-current <slug> --current F --next F [--session] [--expect-version N]` | transactional CAS write of CURRENT/NEXT (secret patterns rejected) |
| `aimem sessions [slug] [--open]` / `aimem session show|close <slug> --session ID [--result] [--note]` | list / inspect / administratively close |
| `aimem txn [slug] [--repair]` | inspect (read-only) or deterministically repair interrupted transactions |
| `aimem recover [slug] [--session ID]` | read-only crash/abandonment scan; writes bounded recovery packets |
| `aimem reconcile <slug>` | memory vs physical classification |
| `aimem fact set|list <slug> ...` | structured provenance |
| `aimem compact <slug>|--all [--dry-run] [--rebuild]` | layered summaries from raw steps |
| `aimem artifact <slug> --path F [--store] [--kind] [--note]` | record artifact; `--store` deduplicates into objects/ |
| `aimem objects [stat|verify]` | object store stats / hash verification |
| `aimem capture <slug>` / `aimem note <slug> --kind K --text T` | refresh REPO_STATE (+facts) / durable notes |
| `aimem search <slug>|--all "<query>" [--limit]` / `aimem context <slug> --query Q --tokens N` / `aimem reindex [slug]` | retrieval |
| `aimem chat-export <slug> --query Q --tokens N [--output F]` / `aimem chat-import <slug> <packet> [--dry-run]` / `aimem import-handoff` | ChatGPT bridge |
| `aimem health [slug] [--verbose] [--json] [--no-repo]` | compact health with exit codes |
| `aimem dashboard [--port N]` / `--status` / `--stop` | local dashboard on 127.0.0.1 only |
| `aimem backup [--label] [--verify] [--output-dir]` / `aimem backups` / `aimem restore <file> --verify` / `aimem restore <file> --confirm` | backup/restore |
| `aimem doctor [--deep] [--repair] [--no-repo] [--slug S]` | diagnostics (mutation only with --repair) |
| `aimem migrate [--dry-run]` | idempotent V3.1.1 -> V4 layout upgrade |
| `aimem integrate <slug> --instructions` / `aimem integrate-global [--dry-run]` | repo pointer / global managed instruction blocks |
| `aimem selftest [--full] [--keep] [-v]` / `aimem stress [--sessions N --steps N]` | isolated end-to-end tests |
| `aimem gc [--days N]` / `aimem-detect [dir]` / `aimem-sweep` | housekeeping / detection / passive physical journal |
