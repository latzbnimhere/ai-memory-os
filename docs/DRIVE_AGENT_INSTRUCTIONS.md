# Exact instructions: reading and updating AI Memory through Google Drive

Drive folder: `My Drive/AI-Memory/<slug>/` (for example `My Drive/AI-Memory/example-project/`).
Local authority: `LOCAL_MEMORY_ROOT` (default `~/AI-Memory`) on the owner's Mac. Drive is a verified mirror, never a
second memory. Fresh physical repo/runtime state overrides both.

## Every agent — reading (the only safe read procedure)

1. Open `AI-Memory/<slug>/MANIFEST.json`. Note `version`, `generation`, `current_version_dir`, `source`
   (memory_version, checkpoint, sha256 of CURRENT/NEXT) and `physical` (repo state **at publish time**).
2. Open `AI-Memory/<slug>/LATEST_HANDOFF.md`. Its `DRIVE_VERSION:` and `GENERATION:` lines must equal the manifest.
   - Equal: continue with top-level files.
   - Different (a publish is in progress), or a file's SHA-256 differs from `files.<name>.sha256` when you can hash:
     read the same names from `AI-Memory/<slug>/<current_version_dir>/` instead. That folder is complete and immutable.
3. Read `NEXT.md`, then only the sections of `CURRENT.md` the task needs (search it; it can be very large).
4. Do NOT load `CHECKPOINTS/`, `VERSIONS/`, `EVIDENCE_INDEX/`, `ARTIFACT_INDEX/`, `DECISIONS.jsonl` or `EVENTS.jsonl`
   wholesale. Open one named file only when a task requires it (e.g. `EVIDENCE_INDEX/INDEX.json` for recent test results).
5. Everything is REPORTED until verified on the real repo/runtime. If physical state differs from the mirror, physical
   state wins; say so explicitly.
6. `LEASE.json` with `"state": "HELD"` and a future `expires_at` = a writer is active; the next version is coming.

## ChatGPT (or any agent without the local `aimem` CLI) — updating

- Never edit, rename or delete anything in the mirror. In particular never touch `MANIFEST.json`, `LEASE.json`,
  `CURRENT.md`, `NEXT.md`, `LATEST_*`, `VERSIONS/`, `CHECKPOINTS/`, or the indexes.
- To hand work back, create exactly one new file:
  `AI-Memory/<slug>/INBOX/<YYYYMMDDTHHMMSSZ>__chatgpt__<short-topic>.md` containing:
  - `BASED_ON_MANIFEST_VERSION: <n>` and `GENERATION: <g>` that you read;
  - VERIFIED facts (with the exact command/result/hash/path that verified them) separated from REPORTED ones;
  - decisions requested from the owner, proposed next actions, errors seen;
  - no passwords, OTPs, API keys, banking numbers, security answers or other secrets; no hidden reasoning.
- The owner's local agent reconciles INBOX proposals into local memory; the next published version reflects them.

## Claude / Codex on the owner's Mac (local CLI) — updating

1. Start: `aimem begin <slug> --agent <claude|codex> --task "<task>" --tokens 4500`.
   Read `DRIVE_STATE=` in the output:
   - `MATCH` / `LOCAL_NEWER` / `UNPUBLISHED`: proceed normally.
   - `DRIVE_NEWER`, `DIVERGED`, or `DRIVE_RECONCILE_REQUIRED=`: run `aimem drive reconcile <slug>`, then
     `aimem drive pull <slug>`; compare with fresh physical state; never copy Drive content over local canonical files.
   - `DRIVE_INBOX=`: `aimem drive pull <slug>` and review `~/AI-Memory/.drive/pulled/<slug>/v*/inbox/`; merge by
     editing local CURRENT/NEXT deliberately (and verify claims physically first).
   - `DRIVE_LEASE=HELD`: another writer is active; do not force anything.
2. Work; log steps with `aimem-step` as usual.
3. Finish: `aimem finish <slug> --session <SESSION_ID> --result <R> --label "<checkpoint>"`.
   It publishes and verifies Drive automatically. Read `DRIVE_SYNC=`:
   - `VERIFIED` / `UP_TO_DATE`: done.
   - `PENDING_CLOUD_ACK` / `COMMITTED_UNACKED`: Drive was slow; run `aimem drive verify <slug>` later (the sweep also
     retries). The previous Drive version stays current meanwhile.
   - `CONFLICT REASON=...`: stop; run `aimem drive reconcile <slug>`; resolve deliberately. Never delete or hand-edit
     the remote manifest to get past it.
4. Manual publish without finishing: `aimem drive push <slug> --agent <name> --session <SESSION_ID>`.
5. Health: `aimem drive status <slug>`, `aimem drive verify <slug>`, `aimem doctor`.

## Never

- Never put secrets in memory, prompts, INBOX files or Drive. The publisher refuses them; do not work around it.
- Never recursively load the Drive archive into context.
- Never treat the mirror as permission for production, App Store, website, marketing or other irreversible actions.
