# Upgrading 4.1.0 -> 4.2.0

4.2.0 is a hardening release. No canonical file format changes; no data is rewritten except the additive,
already-existing `migrate` steps (`engine_version` in `project.json`, config defaults). The SQLite index is
derived and is rebuilt automatically on first use (schema `user_version` 2).

Cloud/CI verification of this release does **not** replace verification on the machine that holds the live
memory root. Do not treat a live root as upgraded until the steps below pass on that machine.

## Behaviour changes to expect

| Before (4.1.0) | Now (4.2.0) |
|---|---|
| finish/checkpoint/write-current proceed while an interrupted transaction is pending | fail closed, exit 4 `UNRESOLVED_TRANSACTIONS`; run `aimem txn <slug> --repair` |
| `txn --repair` rolls forward even if the target was edited after the crash | refuses: `TARGET_CHANGED_SINCE_PREPARE_MANUAL_REVIEW` |
| A CAS-refused checkpoint left an orphan `checkpoints/<id>/` | nothing is created; doctor `--deep` warns about orphans left by older engines |
| CURRENT/NEXT with a token/key pattern could be sealed into a checkpoint | finish/checkpoint refuse (`--allow-secret-pattern` for verified false positives) |
| chat-import stored packets containing secret patterns | refused, report only, exit 3 |
| `pwd=/some/path` counted as a password | not a secret |
| A heartbeat racing `finish` could write the session back as OPEN | serialized by the per-project session lock |
| Unknown/unsafe slugs could create stray dirs (e.g. `aimem note ../x`) | refused (exit 2) before anything is written |
| Two slugs for one repo: detection silently picked one | `aimem-detect` exit 6, `aimem-step` without `--project` refuses |
| Every finish fully rebuilt the project's search index | incremental refresh; `aimem reindex --full` rebuilds |
| Search ran `PRAGMA integrity_check` on every query and did not see new journal entries until the next reindex | no per-query scan; project index refreshed before searching |
| NEXT.md could be dropped from context when CURRENT.md filled the budget; events were raw JSON and truncation dropped the newest | priority budgeting (NEXT/CURRENT always present), one line per record, oldest dropped first |
| `migrate` from an older engine could overwrite newer VERSION/config/manifests | refuses before changing anything |
| Backup restore trusted tar symlinks/hardlinks | only files, dirs and in-root symlinks are extracted |
| Running the unit tests without `AI_MEMORY_ROOT` could resolve paths against `~/AI-Memory` | every test process uses a throwaway root and HOME |

Scripts that parse `REINDEX=PASS files=N chunks=M` still work; the line gained `changed=`, `removed=` and `mode=`.

## Local upgrade sequence (the machine with the live root)

Stop at the first failure. Nothing below edits project repositories.

1. Inspect the live state (read-only): `aimem version`, `aimem status`, `aimem sessions --open`, `aimem txn`,
   `aimem doctor --deep`, `aimem health --verbose`. Record the output.
2. Settle open sessions: finish or administratively close each one with its owner. Promotion refuses to run while
   any session is OPEN. Resolve pending transactions with `aimem txn --repair`.
3. Create a truthful pre-upgrade checkpoint of the memory system itself, and a verified backup:
   `aimem backup --label pre-4.2.0 --verify` (must print `BACKUP_VERIFY=PASS`).
4. Fetch and review the source: `git fetch && git checkout <branch>`, read the diff against the installed version.
5. Test the new engine in isolation from the source checkout:
   `python3 -m unittest discover -s tests -v` (no environment needed; it never touches the live root),
   `python3 bin/aimem selftest --full`, `python3 tools/public_audit.py`.
6. Rehearse against a copy of the live root (the live root is only read):
   `python3 tools/promote.py --rehearse --root ~/AI-Memory [--smoke-slug <registered-slug>]`
   Required: `MIGRATION_REHEARSAL=PASS`, `REHEARSAL_CANONICAL_PRESERVED=True`, `LIVE_ROOT_UNTOUCHED_BY_REHEARSAL=True`.
   Use `--keep` to inspect the upgraded copy: run `doctor --deep`, `context <slug>`, `search <slug> <term>` against it with
   `AI_MEMORY_ROOT=<copy>`. Expect doctor warnings for orphan checkpoints left by 4.1.0 conflicts; they are not errors.
7. Promote: `python3 tools/promote.py --live --root ~/AI-Memory --confirm-live-root ~/AI-Memory --backup-dir <dir>`.
   Note: `--live` also refreshes the managed blocks in `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` (backed up first)
   and restarts the `io.aimemory.sweep` LaunchAgent. Any failed gate after the first mutation rolls back automatically.
8. Verify on the live root: `aimem version` (4.2.0), `aimem doctor --deep`, `aimem health --verbose`,
   `aimem-detect` in each registered repo, `aimem context <slug>` for each project (NEXT present, budget respected),
   `aimem search <slug> <known term>`, compare CURRENT/NEXT hashes, checkpoint lists and journal line counts with step 1.
9. Only then consider the upgrade complete. If anything in step 8 differs unexpectedly, roll back (below).

## Rollback 4.2.0 -> 4.1.0

Automatic: a failed `promote.py` gate restores `bin/`, `lib/`, protocol files, config, PATCH_LEVEL and VERSION from
`<root>/.previous/pre-4.2.0-<stamp>/` and runs the baseline doctor.

Manual, after a completed promotion:
1. Make sure no session is OPEN and no transaction is pending.
2. Copy `<root>/.previous/pre-4.2.0-<stamp>/{bin,lib,VERSION,PATCH_LEVEL,config.json}` and the protocol `*.md` files
   back into the root (move the 4.2.0 `lib/` aside rather than deleting it).
3. Run `aimem doctor --deep` with the restored engine.

Data written by 4.2.0 stays readable by 4.1.0: journals, checkpoints (their extra `memory_version`/`txn` keys are
ignored), `cold/quarantine/` (ignored), and `project.json` (`engine_version` 4.2.0 is informational to 4.1.0). The index
gains a `files` table that 4.1.0 ignores; if 4.2.0 is installed again later it detects the drift and rebuilds.
If memory itself is damaged, restore the verified pre-upgrade backup (`aimem restore <file> --verify`, then `--confirm`).
