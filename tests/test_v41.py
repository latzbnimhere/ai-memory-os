"""Regression tests for AI Memory OS V4.1 adaptive context and lease behavior.

The actual test runs in a child Python process so AI_MEMORY_ROOT is bound
before importing aimem. This keeps it isolated from other unittest modules.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


REPO = Path(__file__).resolve().parents[1]


CHILD = r'''
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import tempfile

with tempfile.TemporaryDirectory(prefix="aimem-v41-test-") as td:
    td = Path(td)
    root = td / "memory"
    repo = td / "repo"

    os.environ["AI_MEMORY_ROOT"] = str(root)

    from aimem import (
        cli,
        context,
        core,
        integrate,
        reconcile,
        sessions,
        sweep,
    )

    repo.mkdir()

    def git(*args):
        r = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
        )
        if r.returncode:
            raise RuntimeError(r.stderr or r.stdout)
        return r.stdout.strip()

    def save_session(j):
        core.atomic_write_json(
            sessions.session_file("unit", j["id"]),
            j,
        )

    git("init", "-q")
    git("config", "user.email", "v41-test@example.invalid")
    git("config", "user.name", "V41 Test")

    (repo / "state.txt").write_text("initial\n")
    git("add", "state.txt")
    git("commit", "-qm", "initial")

    cli.main(["init", str(root)])
    cli.main([
        "register",
        "unit",
        "--name",
        "Unit",
        "--repo",
        str(repo),
    ])

    p = core.project_dir("unit")

    # Give the context builder enough material to exercise its limits.
    (p / "CURRENT.md").write_text(
        "# UNIT CURRENT\n\n"
        + ("authoritative-current-state-line\n" * 1800)
    )

    # Official physical authority baseline.
    cli.main(["capture", "unit"])

    saved = core.try_load_json(
        p / "REPO_STATE.json",
        {},
    ) or {}

    live = core.repo_state_for("unit")

    assert saved.get("head")
    assert saved.get("head") == live.get("head")

    rec = reconcile.reconcile("unit", live)

    assert rec["status"] == "MEMORY_MATCH", rec
    assert not rec.get("flags"), rec

    print("V41_CAPTURE_BASELINE=PASS")

    # ---------------------------------------------------------
    # Adaptive context
    # ---------------------------------------------------------

    smart = context.build_context(
        "unit",
        "",
        4500,
        mode="smart",
        reconciliation=rec,
        live_repo=live,
        pending_txn=[],
    )

    smart_tokens = core.approx_tokens(smart)

    assert "REQUESTED_TOKEN_BUDGET: 4500" in smart
    assert "APPROX_TOKEN_BUDGET: 3600" in smart
    assert smart_tokens <= 3650

    print("V41_SMART_CONTEXT=PASS")

    hot = context.build_context(
        "unit",
        "",
        4500,
        mode="hot",
        reconciliation=rec,
        live_repo=live,
        pending_txn=[],
    )

    hot_tokens = core.approx_tokens(hot)

    assert "APPROX_TOKEN_BUDGET: 3000" in hot
    assert hot_tokens <= 3050

    print("V41_HOT_CONTEXT=PASS")

    deep = context.build_context(
        "unit",
        "",
        6000,
        mode="deep",
        reconciliation=rec,
        live_repo=live,
        pending_txn=[],
    )

    deep_tokens = core.approx_tokens(deep)

    assert "APPROX_TOKEN_BUDGET: 6000" in deep
    assert deep_tokens > smart_tokens
    assert deep_tokens <= 6050

    print("V41_DEEP_CONTEXT=PASS")

    block = integrate.block("claude")

    assert "--tokens 4500" in block
    assert "--mode deep --tokens 6000" in block
    assert "long-running work still active" in block

    print("V41_INTEGRATION_POLICY=PASS")

    # ---------------------------------------------------------
    # Unique ACTIVE session + physical activity = heartbeat
    # ---------------------------------------------------------

    s1, _ = sessions.begin(
        "unit",
        "claude",
        "V4.1 unique active sweep regression",
    )

    sid1 = s1["id"]

    # Seed AUTO_PHYSICAL_STATE. Initial observation is not
    # attributable ongoing activity.
    sweep.main([])

    j1 = sessions.load_session("unit", sid1)

    j1["lease"]["last_heartbeat"] = (
        core.now() - timedelta(minutes=10)
    ).isoformat()

    active_before = j1["lease"]["last_heartbeat"]
    active_count = int(j1["lease"].get("heartbeats", 0))

    save_session(j1)

    (repo / "state.txt").write_text("dirty-one\n")

    sweep.main([])

    after = sessions.load_session("unit", sid1)

    assert (
        core.parse_iso(after["lease"]["last_heartbeat"])
        > core.parse_iso(active_before)
    )

    assert (
        int(after["lease"]["heartbeats"])
        == active_count + 1
    )

    print("V41_UNIQUE_ACTIVE_REFRESH=PASS")

    # ---------------------------------------------------------
    # STALE session must not be revived
    # ---------------------------------------------------------

    git("add", "state.txt")
    git("commit", "-m", "physical-delta-two")

    j1 = sessions.load_session("unit", sid1)

    j1["lease"]["last_heartbeat"] = (
        core.now() - timedelta(hours=3)
    ).isoformat()

    stale_before = j1["lease"]["last_heartbeat"]
    stale_count = int(j1["lease"].get("heartbeats", 0))

    save_session(j1)

    assert sessions.lease_state(j1) == "STALE"

    sweep.main([])

    after_stale = sessions.load_session("unit", sid1)

    assert after_stale["lease"]["last_heartbeat"] == stale_before
    assert int(after_stale["lease"]["heartbeats"]) == stale_count
    assert sessions.lease_state(after_stale) == "STALE"

    print("V41_STALE_NOT_REVIVED=PASS")

    # ---------------------------------------------------------
    # More than one OPEN session = no attribution guess
    # ---------------------------------------------------------

    s2, _ = sessions.begin(
        "unit",
        "codex",
        "V4.1 ambiguous attribution regression",
    )

    sid2 = s2["id"]

    j2 = sessions.load_session("unit", sid2)

    j2["lease"]["last_heartbeat"] = (
        core.now() - timedelta(minutes=10)
    ).isoformat()

    ambiguous_before = j2["lease"]["last_heartbeat"]
    ambiguous_count = int(j2["lease"].get("heartbeats", 0))

    save_session(j2)

    (repo / "state.txt").write_text("dirty-three\n")

    sweep.main([])

    after2 = sessions.load_session("unit", sid2)

    assert after2["lease"]["last_heartbeat"] == ambiguous_before
    assert int(after2["lease"]["heartbeats"]) == ambiguous_count
    assert len(sessions.open_sessions("unit")) == 2

    print("V41_MULTI_SESSION_NO_GUESS=PASS")

    print("V41_TARGETED_REGRESSION=PASS")
'''


class TestV41Regression(unittest.TestCase):
    def test_v41_targeted_behavior(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO / "lib")

        r = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(CHILD)],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
        )

        if r.stdout:
            print(r.stdout)

        if r.stderr:
            print(r.stderr, file=sys.stderr)

        self.assertEqual(
            r.returncode,
            0,
            msg=f"child failed rc={r.returncode}\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}",
        )

        for marker in (
            "V41_CAPTURE_BASELINE=PASS",
            "V41_SMART_CONTEXT=PASS",
            "V41_HOT_CONTEXT=PASS",
            "V41_DEEP_CONTEXT=PASS",
            "V41_INTEGRATION_POLICY=PASS",
            "V41_UNIQUE_ACTIVE_REFRESH=PASS",
            "V41_STALE_NOT_REVIVED=PASS",
            "V41_MULTI_SESSION_NO_GUESS=PASS",
            "V41_TARGETED_REGRESSION=PASS",
        ):
            self.assertIn(marker, r.stdout)
