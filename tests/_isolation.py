"""Process-wide test isolation: never let a test resolve to a real AI Memory root.

`aimem.core` binds AI_MEMORY_ROOT (and HOME-derived defaults such as the backup
directory) at import time. unittest discovery imports every test module into one
interpreter, so whichever module imports `aimem` first decides the root for all
in-process tests. Every test module that imports `aimem` in-process must import
this module FIRST:

    import _isolation  # noqa: F401  (must precede any aimem import)

It forces a throwaway AI_MEMORY_ROOT and HOME before `aimem` is imported and
aborts the whole run if `aimem.core` was already bound to anything else.
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

_ENV_MARKER = "AIMEM_TEST_ISOLATION_DIR"
_PREFIX = "aimem-test-sandbox-"
_TMP = Path(tempfile.gettempdir()).resolve()


def _genuine_sandbox(value):
    """Only a directory this module created (tmp/aimem-test-sandbox-*) may be inherited. Anything else
    (HOME, /, a parent of the live root, a typo) would let tests treat real data as disposable."""
    try:
        p = Path(value).resolve()
    except (OSError, RuntimeError):
        return None
    if p.is_dir() and not Path(value).is_symlink() and p.parent == _TMP and p.name.startswith(_PREFIX):
        return p
    return None


_inherited = _genuine_sandbox(os.environ.get(_ENV_MARKER, ""))
if _inherited is not None:
    # Child processes spawned by tests inherit the parent's isolated sandbox.
    SANDBOX = _inherited
    _owner = False
else:
    SANDBOX = Path(tempfile.mkdtemp(prefix=_PREFIX)).resolve()
    _owner = True
    os.environ[_ENV_MARKER] = str(SANDBOX)
    atexit.register(shutil.rmtree, SANDBOX, True)  # registered before any guard can abort the run

ROOT = SANDBOX / "memory"
HOME = SANDBOX / "home"
HOME.mkdir(parents=True, exist_ok=True)

if "aimem.core" in sys.modules:
    bound = Path(sys.modules["aimem.core"].ROOT).expanduser().resolve()
    if SANDBOX not in bound.parents and bound != SANDBOX:
        raise SystemExit(
            f"TEST_ISOLATION_VIOLATION: aimem.core already bound to {bound} before tests/_isolation.py ran; "
            "refusing to run tests that could touch a real memory root.")
else:
    os.environ["AI_MEMORY_ROOT"] = str(ROOT)
    os.environ["HOME"] = str(HOME)
    # git must still work without the user's global config
    os.environ.setdefault("GIT_CONFIG_NOSYSTEM", "1")

