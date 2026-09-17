# Migration from V3.1.1 to V4
Sequence enforced by `tools/promote.py`: unit tests -> full isolated selftest -> baseline `doctor --deep` on the target root ->
fresh verified backup -> preserve V3.1.1 executables/config/protocol under `<root>/.previous/v3.1.1-<stamp>/` -> install bin/lib/docs/
protocol files -> hash verification -> `aimem migrate` -> `doctor --deep` -> `health` -> functional smoke (begin/step/finish on `ai-memory`)
-> (live only) global managed instruction blocks + LaunchAgent restart. Any failure triggers automatic rollback.
`--rehearse` performs the whole sequence on a temp copy of `~/AI-Memory` and proves canonical files/checkpoints are preserved and
the live root was untouched.
`aimem migrate` (idempotent): creates `objects/`, `.run/`, `.txn`, `cold/summaries`; adds `memory_version`, `engine_version`,
`migrated_from` to project.json; seeds PROVENANCE.jsonl from REPO_STATE/current checkpoint; adds a conservative lease to OPEN
sessions (heartbeat = last step time); config version 4 (keys preserved); VERSION/PATCH_LEVEL with lineage; `.gitignore`; reindex
(FTS schema gains an mtime column). No history is deleted or rewritten.
