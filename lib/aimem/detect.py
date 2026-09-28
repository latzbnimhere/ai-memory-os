"""aimem-detect: print the registered project slug for a directory (exit 1 if none, 6 if ambiguous)."""
from __future__ import annotations

import os
import sys

from . import core


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not core.REGISTRY.exists():
        raise SystemExit(2)
    target = argv[0] if argv else os.getcwd()
    matches = core.detect_candidates(target)
    if len(matches) > 1 and matches[0][0] == matches[1][0]:
        tied = [m[1] for m in matches if m[0] == matches[0][0]]
        print("")
        print(f"AMBIGUOUS_PROJECT: {target} is registered under several slugs with the same repo: {', '.join(tied)}; "
              "pass --project explicitly or fix the registry.", file=sys.stderr)
        raise SystemExit(core.EXIT_AMBIGUOUS)
    slug = core.detect_project(target)
    if not slug:
        print("")
        raise SystemExit(1)
    print(slug)
