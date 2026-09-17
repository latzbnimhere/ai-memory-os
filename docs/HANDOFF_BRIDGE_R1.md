# Global project handoff bridge R1

Local AI Memory persistence is authoritative. Explicit operator configuration
permits publishing only the bounded navigation files described here to their
configured Google Drive desktop root. It does not authorize project production
changes, credential uploads or syncing the AI Memory directory itself.

## Commands

- `aimem handoff`: resolve the current workspace; ambiguous mappings stay pending locally.
- `aimem handoff --project SampleProject`: refresh one mapped project from local authority.
- `aimem handoff --all`: refresh all registered destinations.
- `aimem handoff --status`: show pinned root, local publication statuses and pending count.
- `aimem handoff --retry-pending`: retry queued references and reconcile all current pointers,
  including the crash gap between a valid local checkpoint and hook invocation.
- `aimem handoff --register NewProject --memory-slug registered-slug`: add an explicit mapping
  and its project subfolder beneath the existing pinned root. Omit memory slug for an
  unresolved project. No core-code edit or new root is required.
- `aimem handoff --project SampleProject --set-state /absolute/path/summary.json`:
  store an explicit bounded operational summary. This file is local metadata; it is
  never copied directly to Drive. The renderer publishes only schema-approved fields.

Registry: `registry/handoffs.json`. Required properties: version=1, drive (exact path,
root device/inode, My Drive device/inode), projects (project_id, display_name, folder,
memory_slug or null, active). No wildcard project mapping is implied. The separate AI-Memory-OS entry maps the engine itself.

The discovered root and existing project directories are reused. The bridge never
creates or repairs a missing root. Root/provider identity changes require explicit
re-discovery and registry reconciliation, not an automatic fallback path.

## Structured state

Local `registry/handoff-state/<project_id>.json` has exactly these fields:

```json
{
  "checkpoint": "NEXT_ACCEPTED_CHECKPOINT",
  "current_sha256": "exact SHA256 of the intended CURRENT.md",
  "next_sha256": "exact SHA256 of the intended NEXT.md",
  "phase_status": "PASS",
  "authority": ["Exact workspace, accepted source hash/version and database authority"],
  "service_state": "Exact reported service state with verification timestamp",
  "executed": ["What the phase actually executed"],
  "did_not_execute": ["Explicit exclusions"],
  "blockers": [],
  "owner_decision_required": "NONE",
  "next_safe_action": "One safe next action",
  "hard_boundaries": ["All current project-specific prohibitions"],
  "evidence": {
    "packet_path": "N/A",
    "seal_sha256": "N/A",
    "report_path": "N/A",
    "result_path": "N/A"
  }
}
```

Set `checkpoint` to an exact already-accepted checkpoint ID for initial/manual
publication. Or use NEXT_ACCEPTED_CHECKPOINT after updating canonical CURRENT/NEXT:
when `aimem checkpoint` or `aimem finish` commits that exact content, the hook binds
the summary to the accepted checkpoint ID and publishes it. Invalid, changed or
missing summaries do not become a guessed PASS: they yield NEEDS_RECONCILIATION
with exact missing authority and the actual checkpoint result retained separately.
Canonical edits beyond the accepted snapshot are also explicitly marked.
Checkpoint summaries are not inferred from arbitrary historical prose. Agents should
prepare the bounded summary during phase closeout. Administrative checkpoints that
do not advance current authority refresh the accepted authority rather than replacing it.

## Persistence and failures

The hook runs after the existing canonical transaction, checkpoint metadata,
provenance and local memory commit. It is exception-isolated: a Drive failure cannot
roll back a checkpoint, change its result or stop session closure. Existing begin,
context, step, finish and doctor behavior remains, with optional HANDOFF_SYNC output.
When the registry is absent the hook is a no-op.

`.handoff/pending/` contains durable selector references, not raw reports. Retry
regenerates the latest accepted content, so an old failure cannot overwrite a newer
checkpoint. `.handoff/status/` records SYNCED_LOCAL, FAILED, REFUSED_SECRET or
PROJECT_MAPPING_REQUIRED. Secret refusal removes the pending payload/reference and
keeps a sanitized remediation record; repair the local source before retrying.
If even local recording fails (for example, disk full), the hook prints FAILED and
LOCAL_RECORD_UNAVAILABLE. Retry reconciles registry pointers when storage recovers.

## Atomicity and concurrency

All content is generated and validated in a temporary local staging directory.
A global advisory lock serializes bridge publications across projects. Destination
files use same-directory temporary files, flush/fsync and atomic `os.replace`.
The three project files are CHATGPT_HANDOFF.md (at most 150 lines), LATEST_PACKET.txt
(exactly four fields), and CURRENT_RESULT.json (the last-written commit marker,
including SHA256 for the other two files). Read the marker before and after loading
the files, verify its hashes, and retry if a generation changes or hashes mismatch.
`read_bundle()` implements this rule.

This guarantees complete individual files and detectable/retryable interrupted
sets. It does NOT claim a simultaneous multi-file filesystem transaction or atomic
remote Google Drive delivery. Do not treat a mixed generation as accepted.
Replacing the existing Google Drive project folders with symlinks or swapping their
identities would not preserve reliable desktop sync, so the bridge preserves them.
After a validated project write, the master is rebuilt only from validated project
sets; LAST_SYNC.json is its final marker and binds both master files by SHA256.
If a master write fails, the project stays valid and the pending record remains.
Unrelated existing files are preserved.

SYNCED_LOCAL means verified bytes in the pinned Google Drive desktop folder.
CLOUD_SYNC=UNVERIFIED explicitly distinguishes local publication from server receipt:
Drive may be offline or paused. The bridge uses no Drive API and does not infer
remote acknowledgment from filesystem write success.

## Security and authority

Every rendered project/master is scanned before publication. Rejects include engine
secret patterns, API/private keys, credential assignments, MFA/TOTP/recovery values,
credential-bearing URLs, otpauth URIs and raw environment assignments. Input JSON is
size/type bounded; only the explicit state schema is accepted. No source report,
customer document, private archive, .env or credential file is opened or copied by
the renderer. No secret values are echoed in failure messages or stored in retry logs.
Pattern scanning is not a proof against arbitrary unlabeled secrets; the strict
bounded metadata contract and refusal to copy raw files are essential controls.

Every handoff and index states: CONTEXT/NAVIGATION ONLY; never mutation authority,
never replacement for sealed evidence, physical repo/database state or local memory,
and never permission for an irreversible phase. All original project boundaries
remain in force. Project-specific local workflows remain separately required.

## Verification and rollback

`python3 -m unittest discover -s tests -p test_handoff.py -v` uses temporary roots.
It covers single/all/sequential updates, file interruption and retry, mapping ambiguity,
missing/unavailable Drive, secret rejection, malformed JSON, new registration, master
hash consistency, checkpoint preservation and a real finish lifecycle under failure.
Run existing tests/test_v4.py and `aimem selftest --full` for engine regression checks.

To disable only the bridge, preserve then rename registry/handoffs.json out of its
active name; existing checkpoint behavior continues. To roll back code, use the exact
pre-install copies recorded in the installation manifest. Do not roll back project
checkpoints, change production state or delete existing Drive handoffs.

## Example exclusion policy

A project may be explicitly excluded from handoff publication.

An excluded project must not be read, refreshed, republished, re-registered through a stale mapping, or included in aggregate indexes. Existing local project memory and historical handoff files remain untouched.

Exclusion takes precedence over stale mappings and queued retries. Re-inclusion requires a new explicit operator decision.
