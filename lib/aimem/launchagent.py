"""Locate the macOS LaunchAgent that runs a root's aimem-sweep.

Stdlib only and free of aimem imports: tools/promote.py loads this file directly, without importing the package
(importing aimem binds AI_MEMORY_ROOT at import time).

New installations use DEFAULT_LABEL. Installations that predate the open-source release run the same sweep under
their own label, so the agent is identified by what it runs (<root>/bin/aimem-sweep), never by its name alone.
"""
from __future__ import annotations

import os
import plistlib
from pathlib import Path

DEFAULT_LABEL = "io.aimemory.sweep"


def agents_dir(home=None):
    return Path(home if home is not None else Path.home()) / "Library" / "LaunchAgents"


def _runs(pl, sweep):
    args = pl.get("ProgramArguments") or []
    if not isinstance(args, list):
        args = []
    if isinstance(pl.get("Program"), str):
        args = args + [pl["Program"]]
    return any(isinstance(a, str) and os.path.realpath(a) == sweep for a in args)


def find(root, home=None):
    """(label, plist path) of the LaunchAgent running <root>/bin/aimem-sweep, or (DEFAULT_LABEL, its plist path)
    when none is installed. With several matches DEFAULT_LABEL wins, then the first plist by file name."""
    d = agents_dir(home)
    sweep = os.path.realpath(os.path.join(str(root), "bin", "aimem-sweep"))
    found = []
    try:
        plists = sorted(d.glob("*.plist"))
    except OSError:
        plists = []
    for p in plists:
        try:
            pl = plistlib.loads(p.read_bytes())
        except Exception:  # noqa: BLE001  (unreadable or foreign plist: not ours)
            continue
        if isinstance(pl, dict) and isinstance(pl.get("Label"), str) and _runs(pl, sweep):
            found.append((pl["Label"], p))
    for label, p in found:
        if label == DEFAULT_LABEL:
            return label, p
    return found[0] if found else (DEFAULT_LABEL, d / f"{DEFAULT_LABEL}.plist")


def is_loaded(label, launchctl_list_output):
    """Exact label match on `launchctl list` output (PID<TAB>status<TAB>label lines)."""
    return any(line.rsplit("\t", 1)[-1].strip() == label for line in launchctl_list_output.splitlines())
