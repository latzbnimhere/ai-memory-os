# Backup and restore

`aimem backup [--label L] [--verify]` -> `~/AI-Memory-Backups/AI-Memory-<stamp>[-L].tar.gz` + `.sha256` + `.meta.json`.
Contents: whole root except `.locks`, `.run`, `.generated`, staged/tmp files and the derived SQLite index; an embedded
`BACKUP_MANIFEST.sha256` lists every file hash.
`aimem backups` lists age, size, engine version and last verification result.
Every file is hashed from exactly the bytes archived, so a concurrent append cannot make the manifest disagree with the
tarball. In-root symlinks are kept as (relative) links. Symlinks pointing OUTSIDE the root (e.g. a project directory kept on
another volume) are dereferenced: their content is archived and restored as real files, so a restore brings the data back.
Dangling links, links nested inside such external content, links to an ancestor of the root and names containing a newline
are skipped. Both lists are in the `.meta.json` sidecar (`skipped_or_dereferenced`) and printed as warnings.
`aimem restore <file> --verify`: tarball hash -> safe members (only regular files, directories, symlinks that resolve inside
`AI-Memory/` and hardlinks to other members; escaping links, devices and FIFOs fail verification) -> extract to temp -> every manifest hash -> `doctor --deep --no-repo`
on the restored copy -> PASS/FAIL (`.verify.json` sidecar) -> temp deleted. A tarball is not "healthy" until this passes.
`aimem restore <file> --confirm`: verifies first, then moves the live root to `~/AI-Memory.pre-restore-<stamp>` (never deleted)
and extracts the backup in its place (if the live root is missing, it is simply recreated). Extraction is member by
member with on-disk path checks, independent of the Python version's tarfile filters: duplicate names, members below a
symlink member, links that resolve outside the target and hardlinks to non-members are refused. Without `--confirm`
nothing is mutated. After restore run `aimem reindex` and `aimem doctor --deep`.
Health reports `backup_status` (OK/OLD × VERIFIED/UNVERIFIED, NONE) with a 7-day default threshold.
Known-good V3.1.1 baseline: `~/AI-Memory-Backups/v3.1.1-known-good-<stamp>/` (never delete).
