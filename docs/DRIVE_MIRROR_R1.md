# AI Memory Drive mirror R1 (`aimem drive`)

An extension of the existing AI Memory OS. `LOCAL_MEMORY_ROOT` (default `~/AI-Memory`) stays the only authoritative
memory. Google Drive `My Drive/AI-Memory/` is a verified mirror and coordination layer so agents without the local CLI
(ChatGPT through a Drive connector, cloud agents) can read the latest bounded project state, and local CLI agents
(Claude, Codex) can publish verified updates. Drive never writes canonical local memory. There is no second
authoritative root.

Authority order: explicit owner instruction > freshly verified physical repo/runtime state > local CURRENT/NEXT >
latest local checkpoint > Drive mirror.

## Layout

```
My Drive/AI-Memory/
  README_AGENTS.md          agent protocol (same text as <slug>/PROMPTS/DRIVE_AGENT_PROTOCOL.md)
  PROJECTS.json             navigation index (not authoritative; verify the project MANIFEST.json)
  <slug>/
    MANIFEST.json           commit marker: version, generation, source hashes, physical state, file sha256/md5/drive_id
    LATEST_HANDOFF.md       bounded entry point: DRIVE_VERSION/GENERATION, authority law, bridge render, context pack
    CURRENT.md  NEXT.md     verbatim copies of local canonical files
    LATEST_CHECKPOINT.json  local checkpoint meta + integrity result
    DECISIONS.jsonl  EVENTS.jsonl   verbatim local journals (reference; never load wholesale)
    LEASE.json              single-writer lease
    PROMPTS/                protocol, CONTINUE.md, local prompts/, legacy handoff instructions, Google Doc references
    CHECKPOINTS/            INDEX.jsonl of every local checkpoint + immutable copies of authoritative ones
    EVIDENCE_INDEX/         INDEX.json (latest facts, recent command/test/error/result steps), PROVENANCE.jsonl, cold/
    ARTIFACT_INDEX/         INDEX.json (name, sha256, bytes, local path), ARTIFACTS.jsonl, cold/
    VERSIONS/vNNNNNN-<gen>/ immutable complete hot set of each committed version (+ VERSION.json)
    INBOX/                  proposals from agents without the CLI; quarantined out-of-protocol edits
```

Large binary evidence is never uploaded by default: indexes carry hashes, sizes and local paths. `aimem drive attach`
uploads one file to a `cold/` folder explicitly (size-capped, secret-scanned when textual).

## Identity (drivefs-item-id-v1)

The Google account in the CloudStorage mount name plus server-assigned Google Drive folder ids read from the DriveFS
extended attribute `com.google.drivefs.item-id#S` (My Drive, `AI-Memory`, each project folder). Device and inode
numbers are never used: they change on every DriveFS remount or Mac reboot, which is what broke the handoff bridge on
2026-09-29 (the device number changed after a reboot; the 4.2.0 promotion had reverted the earlier item-id fix). A replaced or
same-named folder has a different id and fails closed (`DRIVE_IDENTITY_MISMATCH`); an unreadable id fails closed
(`DRIVE_IDENTITY_UNVERIFIABLE`). Re-pinning a changed root requires `aimem drive pin --accept-new-root <id>`.
The handoff bridge uses the same module (`aimem handoff --pin-drive`).

## Remote readback

A write is verified in two stages: (1) mount readback, sha256 of the bytes on the DriveFS mount; (2) server
acknowledgement, DriveFS's own copy of the server metadata (`mirror_metadata_sqlite.db`, opened as a private APFS clone
plus WAL copy, read-only, no network) must show, for the exact path under the pinned folder id, a server file id, the
local size and the local md5. Schema changes, ambiguity or unreadable data yield UNVERIFIED, never a pass.

## Publish protocol (`aimem drive push <slug>`, also run by `aimem finish` / `aimem checkpoint`)

1. Local flock `drive.<slug>`, then Drive `LEASE.json` (live foreign lease -> `LEASE_HELD`, exit 3; an expired lease
   is a stale writer and is taken over with a `STALE_WRITER_LEASE_TAKEN_OVER` note).
2. Read remote MANIFEST.json and classify against `registry/drive-state/<slug>.json`:
   `REMOTE_NEWER`, `REMOTE_REWRITTEN`, `REMOTE_ROLLED_BACK`, `CONFLICT_FOREIGN_WRITER`, `DIVERGED`,
   `REMOTE_MANIFEST_MISSING`, `REMOTE_MANIFEST_MALFORMED` all refuse (exit 3) and write nothing.
3. Consistent local snapshot under the project write lock; fresh physical repo state and reconciliation are recorded.
4. Secret scan of every staged file; any hit refuses the whole publish (labels only, values never echoed).
5. Write `VERSIONS/vN-<gen>/` then top-level files; mount readback.
6. Server acknowledgement of every content file.
7. Compare-and-swap: remote manifest unchanged (`STALE_WRITER` otherwise), lease still ours (`LEASE_LOST`), local
   CURRENT/NEXT unchanged (`LOCAL_CHANGED_DURING_PUSH`).
8. Only then write MANIFEST.json, read it back, wait for its server acknowledgement -> `VERIFIED`
   (or `COMMITTED_UNACKED`, completed later by `aimem drive verify`).

A timeout before step 8 returns `PENDING_CLOUD_ACK`; the previous version stays current and a pending marker is left
for `aimem drive push` / the sweep LaunchAgent. Files changed on Drive outside the protocol are copied to
`INBOX/QUARANTINE_*` before being replaced, never silently lost. Journals and indexes are replaced whole; old versions
remain in `VERSIONS/`.

## What is published (source mode)

`--source auto` (default): the live canonical CURRENT/NEXT, unless ANOTHER active session holds uncheckpointed edits
in progress; then the accepted (immutable) current checkpoint is published, the manifest records
`source.mode=checkpoint` and the note `UNCHECKPOINTED_LOCAL_EDITS_NOT_PUBLISHED`, and that session's own `finish`
publishes its accepted result. `--source canonical|checkpoint` forces one. Comparison states: `MATCH`,
`MATCH_ACCEPTED_CHECKPOINT` (Drive equals the accepted checkpoint; local has edits in progress), `LOCAL_NEWER`,
`DRIVE_NEWER`, `DIVERGED`, `UNPUBLISHED`.

## Commands

| Command | Effect |
|---|---|
| `aimem drive pin [--account A] [--accept-new-root ID]` | pin/re-verify `My Drive/AI-Memory` by Drive ids |
| `aimem drive register <slug> [--folder F]` | create/pin the project folder (waits for its server id) |
| `aimem drive status <slug>` | read-only: identity, remote version, local comparison, lease, inbox, pending |
| `aimem drive push <slug> [--agent A --session S] [--timeout s] [--no-wait] [--backfill-checkpoints N]` | atomic publish |
| `aimem drive pull <slug>` | verified copy of the committed hot set + INBOX into `<root>/.drive/pulled/`; never touches canonical memory |
| `aimem drive verify <slug>` | identity, manifest, mount hashes, server acknowledgement, stable drive ids, version dir, secrets, local match |
| `aimem drive reconcile <slug> [--apply]` | physical vs Drive, local vs Drive, recommended action; `--apply` only pushes/verifies, never merges |
| `aimem drive attach <slug> --file F --kind evidence\|artifact` | explicit cold upload |
| `aimem drive lease <slug> [--release-expired]` | show the lease; release only an expired one |

Engine hooks: `aimem begin` prints `DRIVE_STATE=MATCH|LOCAL_NEWER|DRIVE_NEWER|DIVERGED|UNPUBLISHED|UNAVAILABLE`
(read-only, 15 s bound) plus `DRIVE_RECONCILE_REQUIRED`, `DRIVE_INBOX`, `DRIVE_LEASE`, `DRIVE_PENDING` when relevant.
`aimem finish` prints `DRIVE_SYNC=VERIFIED|UP_TO_DATE|PENDING_CLOUD_ACK|COMMITTED_UNACKED|CONFLICT|FAILED`; a Drive
failure never undoes the local checkpoint. Projects not registered with `aimem drive register` are unaffected.
`aimem doctor` fails if a Drive registry pins device/inode identity or an upgrade dropped the Drive modules.

Settings (`registry/drive-mirror.json` `settings`): `cloud_ack_timeout_s` 300, `finish_ack_timeout_s` 120,
`sweep_ack_timeout_s` 60, `lease_ttl_s` 1800, `begin_check` true, `finish_publish` true, `handoff_context_tokens` 6000.

## Security

Never published: passwords, OTPs, API keys/tokens, private keys, credential URLs, IBAN (checksum-validated), payment
card numbers (Luhn), security answers, otpauth URIs and the engine's existing secret patterns. Refusal, not redaction.
Prompts and operational instructions are preserved; hidden reasoning never is (the engine never records it).

## Rollback

Disable publication: set `settings.finish_publish` / `begin_check` false, or rename `registry/drive-mirror.json`.
Engine rollback: restore the files saved in `.previous/pre-drive-mirror-r1-*`. Drive content can stay; it is a mirror.
