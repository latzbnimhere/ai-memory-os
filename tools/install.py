#!/usr/bin/env python3
"""Fail-closed fresh installer for AI Memory OS.

This installer is only for a brand-new runtime root. It never upgrades or
mutates an existing installation.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1]
BIN_FILES = ("aimem", "aimem-detect", "aimem-step", "aimem-sweep")
SHARE_FILES = (
    "AI_MEMORY_AGENT_PROTOCOL_V4.md",
    "AI_MEMORY_AUTOPILOT.md",
    "AI_MEMORY_CONTINUOUS_JOURNAL.md",
    "OTHER_AI_AGENT_INSTRUCTIONS.md",
    "SESSION_BINDING_V3_1.md",
    "AI_PROTOCOL.md",
)


def fail(message, code=2):
    print(f"INSTALL=FAIL reason={message}", file=sys.stderr)
    return code


def run_checked(cmd, env):
    result = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        raise RuntimeError(f"command failed rc={result.returncode}: {' '.join(map(str, cmd))}\n{output[-3000:]}")
    return result.stdout.strip()


def install(root: Path):
    source = SOURCE.resolve()
    root = root.expanduser().resolve()

    if root.exists():
        raise RuntimeError("target already exists; fresh install refuses existing paths")
    if root == source or source in root.parents:
        raise RuntimeError("runtime root must be outside the source repository")

    created = False
    try:
        root.mkdir(parents=True, exist_ok=False)
        created = True

        (root / "bin").mkdir()
        for name in BIN_FILES:
            src = source / "bin" / name
            if not src.is_file():
                raise RuntimeError(f"missing source launcher: bin/{name}")
            dst = root / "bin" / name
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)

        shutil.copytree(
            source / "lib",
            root / "lib",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

        for name in SHARE_FILES:
            src = source / "share" / name
            if not src.is_file():
                raise RuntimeError(f"missing protocol file: share/{name}")
            shutil.copy2(src, root / name)

        docs_v4 = root / "docs" / "v4"
        shutil.copytree(source / "docs", docs_v4)
        shutil.copy2(source / "README.md", docs_v4 / "README.md")
        shutil.copy2(source / "VERSION", root / "VERSION")

        env = dict(os.environ, AI_MEMORY_ROOT=str(root))
        aimem = root / "bin" / "aimem"
        run_checked([sys.executable, str(aimem), "init", str(root)], env)
        run_checked([sys.executable, str(aimem), "doctor", "--deep", "--no-repo"], env)
        version = run_checked([sys.executable, str(aimem), "version"], env)

        print(f"INSTALL=PASS root={root}")
        print(f"VERIFY={version.splitlines()[0] if version else 'PASS'}")
        print("GLOBAL_INTEGRATION=NOT_PERFORMED")
        print("LAUNCH_SERVICES=NOT_MODIFIED")
        return 0
    except Exception as exc:
        if created and root.exists():
            shutil.rmtree(root, ignore_errors=True)
        raise RuntimeError(str(exc))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fresh-install AI Memory OS into a new explicit runtime root")
    parser.add_argument("--root", required=True, help="New runtime root. It must not already exist.")
    args = parser.parse_args(argv)

    try:
        return install(Path(args.root))
    except Exception as exc:
        return fail(str(exc), 1)


if __name__ == "__main__":
    raise SystemExit(main())
