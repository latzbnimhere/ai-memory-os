# Backup and restore

`aimem backup [--label L] [--verify]` -> `~/AI-Memory-Backups/AI-Memory-<stamp>[-L].tar.gz` + `.sha256` + `.meta.json`.
Contents: whole root except `.locks`, `.run`, `.generated`, staged/tmp files and the derived SQLite index; an embedded
`BACKUP_MANIFEST.sha256` lists every file hash.
`aimem backups` lists age, size, engine version and last verification result.
`aimem restore <file> --verify`: tarball hash -> safe paths -> extract to temp -> every manifest hash -> `doctor --deep --no-repo`
on the restored copy -> PASS/FAIL (`.verify.json` sidecar) -> temp deleted. A tarball is not "healthy" until this passes.
`aimem restore <file> --confirm`: verifies first, then moves the live root to `~/AI-Memory.pre-restore-<stamp>` (never deleted)
and extracts the backup in its place. Without `--confirm` nothing is mutated. After restore run `aimem reindex` and `aimem doctor --deep`.
Health reports `backup_status` (OK/OLD × VERIFIED/UNVERIFIED, NONE) with a 7-day default threshold.
Known-good V3.1.1 baseline: `~/AI-Memory-Backups/v3.1.1-known-good-<stamp>/` (never delete).
