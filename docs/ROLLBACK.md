# Rollback to V3.1.1
Automatic: `tools/promote.py` restores `bin/*`, protocol files, config, PATCH_LEVEL from `<root>/.previous/v3.1.1-<stamp>/`,
moves `lib/` aside, restores global instruction files from the promotion backups, and runs the baseline doctor.
Manual: follow `~/AI-Memory-Backups/v3.1.1-known-good-<stamp>/ROLLBACK_PROCEDURE.md` (executables/config/instructions/LaunchAgent,
or full restore of the tarball if memory itself is damaged). Project repositories are never rolled back. Evidence/logs are preserved.
Files added by V4 (PROVENANCE.jsonl, objects/, cold/summaries, memory_version in project.json) are ignored by V3.1.1 and harmless.
