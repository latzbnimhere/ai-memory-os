"""Claude Code hooks: automatic session begin, step logging, close, and settings.json management.

Every test drives the real launcher in a child process with an isolated AI_MEMORY_ROOT and HOME,
feeding hook payloads on stdin exactly as Claude Code does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AIMEM = REPO / "bin" / "aimem"
CLAUDE_SID = "0b1e2c3d-hooks-test"


class HooksTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aimem-hooks-")
        self.addCleanup(self.tmp.cleanup)
        td = Path(self.tmp.name)
        self.root, self.home, self.repo = td / "memory", td / "home", td / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.env = dict(os.environ, AI_MEMORY_ROOT=str(self.root), HOME=str(self.home))
        for k in ("AIMEM_SESSION_ID", "AIMEM_HOOKS_DISABLE"):
            self.env.pop(k, None)
        self.git("init", "-q")
        self.git("config", "user.email", "hooks-test@example.invalid")
        self.git("config", "user.name", "Hooks Test")
        (self.repo / "state.txt").write_text("initial\n")
        self.git("add", "state.txt")
        self.git("commit", "-qm", "initial")
        self.aimem("init", str(self.root))
        self.aimem("register", "unit", "--name", "Unit", "--repo", str(self.repo))

    def git(self, *args):
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True)

    def aimem(self, *args, stdin=None, cwd=None):
        r = subprocess.run([sys.executable, str(AIMEM), *args], input=stdin, env=self.env, cwd=str(cwd or self.repo),
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"{args}\n{r.stdout}\n{r.stderr}")
        return r

    def hook(self, event, sid=CLAUDE_SID, cwd=None, **fields):
        payload = {"session_id": sid, "hook_event_name": event, "cwd": str(cwd or self.repo), "transcript_path": "/dev/null",
                   "permission_mode": "default", **fields}
        return self.aimem("hooks", "run", stdin=json.dumps(payload))

    def sessions(self):
        d = self.root / "projects" / "unit" / "sessions"
        if not d.exists():
            return []
        return [json.loads(f.read_text()) for f in sorted(d.glob("*.json")) if not f.name.endswith(".steps.jsonl")]

    def open_sessions(self):
        return [j for j in self.sessions() if j.get("status") == "OPEN"]

    def steps(self, sid):
        f = self.root / "projects" / "unit" / "sessions" / f"{sid}.steps.jsonl"
        if not f.exists():
            return []
        return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


class TestSessionLifecycle(HooksTestCase):
    def test_session_start_opens_bound_session_and_prints_bounded_context(self):
        r = self.hook("SessionStart", source="startup")
        self.assertIn("# GENERATED AI CONTEXT PACK", r.stdout)
        self.assertIn("AIMEM_HOOKS=ACTIVE", r.stdout)
        self.assertIn("AIMEM_PROJECT=unit", r.stdout)
        self.assertLessEqual(len(r.stdout), 10_000)
        opens = self.open_sessions()
        self.assertEqual(len(opens), 1)
        self.assertEqual(opens[0]["agent"], "claude")
        self.assertIn(f"AIMEM_SESSION_ID={opens[0]['id']}", r.stdout)
        binding = json.loads((self.root / ".run" / "hooks" / "claude" / f"{CLAUDE_SID}.json").read_text())
        self.assertEqual(binding["session_id"], opens[0]["id"])

    def test_prompt_tools_and_failures_are_logged_against_the_bound_session(self):
        self.hook("SessionStart", source="startup")
        sid = self.open_sessions()[0]["id"]
        self.hook("UserPromptSubmit", prompt="Fix the flaky login test\nand nothing else")
        self.hook("PostToolUse", tool_name="Read", tool_input={"file_path": str(self.repo / "state.txt")}, tool_response={})
        self.hook("PostToolUse", tool_name="Edit", tool_input={"file_path": str(self.repo / "state.txt"), "old_string": "a", "new_string": "b"},
                  tool_response={"filePath": str(self.repo / "state.txt")})
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "python3 -m pytest -q", "description": "Run the test suite"},
                  tool_response={"stdout": "ok", "stderr": "", "interrupted": False})
        fake_key = "sk-" + "abcdefghij" * 3  # assembled at runtime so the public tree audit never sees a key-shaped literal
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": f"curl -H 'Authorization: Bearer {fake_key}' https://example.com"},
                  tool_response={"stdout": "", "stderr": "", "interrupted": False})
        self.hook("PostToolUseFailure", tool_name="Bash", tool_input={"command": "make build"}, error="exit status 2: missing dependency")
        self.assertEqual(len(self.open_sessions()), 1, "hooks must not open a second session")
        session = self.open_sessions()[0]
        self.assertEqual(session["task"], "Fix the flaky login test and nothing else")
        steps = self.steps(sid)
        kinds = [s["step_kind"] for s in steps]
        self.assertEqual(kinds, ["plan", "write", "test", "command", "error"], steps)
        self.assertTrue(all(s["session_binding"] == "hook" and s["agent"] == "claude" for s in steps))
        self.assertEqual(steps[1]["files"], [str(self.repo / "state.txt")])
        self.assertEqual(steps[1]["summary"], "Edit state.txt")
        self.assertEqual(steps[2]["summary"], "Run the test suite")
        self.assertNotIn(fake_key, json.dumps(steps))
        self.assertIn("[REDACTED_SECRET]", steps[3]["command"])
        self.assertEqual(steps[4]["result"], "ERROR")
        self.assertIn("missing dependency", steps[4]["summary"])
        # errors are durable events too
        events = (self.root / "projects" / "unit" / "EVENTS.jsonl").read_text()
        self.assertIn("missing dependency", events)

    def test_stop_heartbeats_and_compact_reuses_the_same_session(self):
        self.hook("SessionStart", source="startup")
        before = self.open_sessions()[0]
        self.hook("Stop", stop_hook_active=False)
        after = self.open_sessions()[0]
        self.assertGreater(after["lease"]["heartbeats"], before["lease"]["heartbeats"])
        self.hook("PreCompact", trigger="auto")
        r = self.hook("SessionStart", source="compact")
        self.assertIn(f"AIMEM_SESSION_ID={before['id']}", r.stdout)
        self.assertIn("# GENERATED AI CONTEXT PACK", r.stdout)
        self.assertEqual(len(self.open_sessions()), 1)
        self.assertEqual([s["step_kind"] for s in self.steps(before["id"])], ["other"])

    def test_session_end_closes_unfinished_and_next_start_reports_it(self):
        self.hook("SessionStart", source="startup")
        sid = self.open_sessions()[0]["id"]
        self.hook("PostToolUse", tool_name="Write", tool_input={"file_path": str(self.repo / "new.txt"), "content": "x"}, tool_response={})
        self.hook("SessionEnd", reason="prompt_input_exit")
        self.assertEqual(self.open_sessions(), [])
        closed = [j for j in self.sessions() if j["id"] == sid][0]
        self.assertEqual(closed["result"], "UNFINISHED")
        self.assertTrue(closed["closed_administratively"])
        self.assertFalse((self.root / ".run" / "hooks" / "claude" / f"{CLAUDE_SID}.json").exists())
        # a new Claude Code session gets a fresh aimem session and a pointer to the unfinished one
        r = self.hook("SessionStart", sid="second-claude-session", source="startup")
        self.assertIn(f"PREVIOUS_SESSION_UNFINISHED={sid}", r.stdout)
        self.assertEqual(len(self.open_sessions()), 1)
        self.assertNotEqual(self.open_sessions()[0]["id"], sid)

    def test_session_without_meaningful_steps_closes_as_empty(self):
        self.hook("SessionStart", source="startup")
        self.hook("UserPromptSubmit", prompt="what does this repo do?")
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.sessions()[0]["result"], "EMPTY")

    def test_late_binding_when_hooks_missed_session_start(self):
        self.hook("PostToolUse", tool_name="Bash", tool_input={"command": "ls"}, tool_response={"stdout": "", "stderr": "", "interrupted": False})
        opens = self.open_sessions()
        self.assertEqual(len(opens), 1)
        self.assertEqual([s["step_kind"] for s in self.steps(opens[0]["id"])], ["command"])

    def test_manual_finish_closes_session_and_hooks_do_not_resurrect_it_on_stop(self):
        self.hook("SessionStart", source="startup")
        sid = self.open_sessions()[0]["id"]
        (self.root / "projects" / "unit" / "CURRENT.md").write_text("# CURRENT\n\nDone.\n")
        self.aimem("finish", "unit", "--session", sid, "--result", "PASS", "--label", "manual")
        self.hook("Stop", stop_hook_active=False)
        self.hook("SessionEnd", reason="other")
        self.assertEqual(self.open_sessions(), [])
        self.assertEqual([j for j in self.sessions() if j["id"] == sid][0]["result"], "PASS")

    def test_stdout_is_shortened_to_the_claude_code_cap(self):
        (self.root / "projects" / "unit" / "CURRENT.md").write_text("# UNIT CURRENT\n\n" + ("authoritative-current-state-line\n" * 1800))
        r = self.hook("SessionStart", source="startup")
        self.assertLessEqual(len(r.stdout), 10_000, len(r.stdout))
        self.assertIn("AIMEM_HOOKS=ACTIVE", r.stdout)
        self.assertIn("FULL_CONTEXT=", r.stdout)
        full = Path(r.stdout.split("FULL_CONTEXT=", 1)[1].split(" ", 1)[0])
        self.assertTrue(full.exists())
        self.assertGreater(len(full.read_text()), len(r.stdout))


class TestFailOpen(HooksTestCase):
    def test_unregistered_cwd_is_a_silent_noop(self):
        with tempfile.TemporaryDirectory() as other:
            r = self.hook("SessionStart", cwd=other, source="startup")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.sessions(), [])

    def test_malformed_and_empty_stdin_never_fail(self):
        for stdin in ("", "not json", "[1,2]", '{"hook_event_name": "SessionStart"}'):
            r = self.aimem("hooks", "run", stdin=stdin)
            self.assertEqual(r.stdout, "", stdin)
        self.assertEqual(self.sessions(), [])

    def test_engine_error_is_reported_on_stderr_with_exit_zero(self):
        (self.root / "projects" / "unit" / "project.json").write_text("{ broken")
        r = self.hook("SessionStart", source="startup")
        self.assertEqual(r.stdout, "")
        self.assertIn("AIMEM_HOOK=SKIPPED event=SessionStart", r.stderr)
        self.assertEqual(self.sessions(), [])

    def test_disable_switch(self):
        self.env["AIMEM_HOOKS_DISABLE"] = "1"
        r = self.hook("SessionStart", source="startup")
        self.assertEqual(r.stdout, "")
        self.assertEqual(self.sessions(), [])


class TestSettingsManagement(HooksTestCase):
    def setUp(self):
        super().setUp()
        self.target = self.home / ".claude" / "settings.json"

    def hooks_in(self):
        return json.loads(self.target.read_text())

    def test_install_creates_file_and_is_idempotent(self):
        r = self.aimem("hooks", "install")
        self.assertIn(f"HOOKS_INSTALL=CREATED target={self.target}", r.stdout)
        self.assertIn("HOOKS_STATUS=INSTALLED events=7/7", r.stdout)
        settings = self.hooks_in()
        self.assertEqual(sorted(settings["hooks"]), sorted(["SessionStart", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure", "Stop", "PreCompact", "SessionEnd"]))
        cmd = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        self.assertIn(str(self.root), cmd)
        self.assertTrue(cmd.endswith("hooks run"))
        self.assertEqual(settings["hooks"]["PostToolUse"][0]["matcher"], "^(Edit|MultiEdit|Write|NotebookEdit|Bash)$")
        self.assertGreaterEqual(settings["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"], 5)
        r = self.aimem("hooks", "install")
        self.assertIn("HOOKS_INSTALL=UNCHANGED", r.stdout)
        self.assertEqual(len(self.hooks_in()["hooks"]["SessionStart"]), 1)
        self.assertEqual(list(self.target.parent.glob("*.bak")), [])

    def test_install_preserves_foreign_settings_and_hooks_and_uninstall_removes_only_ours(self):
        self.target.parent.mkdir()
        original = {
            "permissions": {"allow": ["Bash(git status)"]},
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": "echo mine"}]}],
                "PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo theirs"},
                                                               {"type": "command", "command": "/old/AI-Memory/bin/aimem hooks run"}]}],
                "Notification": [{"hooks": [{"type": "command", "command": "say hi"}]}],
            },
        }
        self.target.write_text(json.dumps(original))
        r = self.aimem("hooks", "install", "--dry-run")
        self.assertIn("HOOKS_INSTALL=DRY_RUN", r.stdout)
        self.assertEqual(json.loads(self.target.read_text()), original)
        r = self.aimem("hooks", "install")
        self.assertIn("HOOKS_INSTALL=UPDATED", r.stdout)
        self.assertIn("backup=settings.json.pre-aimem-hooks-", r.stdout)
        s = self.hooks_in()
        self.assertEqual(s["permissions"], original["permissions"])
        self.assertEqual(s["hooks"]["Notification"], original["hooks"]["Notification"])
        self.assertEqual([h["command"] for h in s["hooks"]["SessionStart"][0]["hooks"]], ["echo mine"])
        self.assertTrue(s["hooks"]["SessionStart"][1]["hooks"][0]["command"].endswith("hooks run"))
        post = s["hooks"]["PostToolUse"]
        self.assertEqual([h["command"] for h in post[0]["hooks"]], ["echo theirs"], "stale aimem entry from another root is replaced")
        self.assertEqual(len(post), 2)
        r = self.aimem("hooks", "status")
        self.assertIn("HOOKS_STATUS=INSTALLED", r.stdout)
        r = self.aimem("hooks", "uninstall")
        self.assertIn("HOOKS_UNINSTALL=UPDATED", r.stdout)
        s = self.hooks_in()
        self.assertEqual(s["permissions"], original["permissions"])
        self.assertEqual(s["hooks"]["SessionStart"], original["hooks"]["SessionStart"])
        self.assertEqual([h["command"] for h in s["hooks"]["PostToolUse"][0]["hooks"]], ["echo theirs"])
        self.assertEqual(s["hooks"]["Notification"], original["hooks"]["Notification"])
        self.assertNotIn("SessionEnd", s["hooks"])
        r = self.aimem("hooks", "status")
        self.assertIn("HOOKS_STATUS=NOT_INSTALLED", r.stdout)

    def test_invalid_settings_file_is_refused_untouched(self):
        self.target.parent.mkdir()
        self.target.write_text("{ not json")
        r = subprocess.run([sys.executable, str(AIMEM), "hooks", "install"], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid JSON", r.stderr)
        self.assertEqual(self.target.read_text(), "{ not json")

    def test_show_prints_snippet_and_target_override(self):
        r = self.aimem("hooks", "show")
        snippet = json.loads(r.stdout)
        self.assertEqual(len(snippet["hooks"]), 7)
        project_settings = self.repo / ".claude" / "settings.json"
        r = self.aimem("hooks", "install", "--target", str(project_settings))
        self.assertIn("HOOKS_INSTALL=CREATED", r.stdout)
        self.assertTrue(project_settings.exists())
        self.assertFalse(self.target.exists())
        r = self.aimem("hooks", "status", "--target", str(project_settings))
        self.assertIn("HOOKS_STATUS=INSTALLED", r.stdout)


if __name__ == "__main__":
    unittest.main()
