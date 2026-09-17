"""aimem-detect: print the registered project slug for a directory (exit 1 if none)."""
from __future__ import annotations

import os
import sys

from . import core


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not core.REGISTRY.exists():
        raise SystemExit(2)
    target = argv[0] if argv else os.getcwd()
    slug = core.detect_project(target)
    if not slug:
        print("")
        raise SystemExit(1)
    print(slug)
