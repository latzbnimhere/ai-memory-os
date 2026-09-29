"""Isolated tests for the verified Drive mirror (aimem drive) and the stable drive identity. No real Drive writes:
a temporary CloudStorage tree stands in for DriveFS; Drive ids and server acknowledgement are simulated."""
import _isolation  # noqa: F401  (must precede any aimem import; see tests/_isolation.py)
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aimem import cli, core, drive, driveid, sessions


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo)] + list(a), capture_output=True, text=True).stdout.strip()


def quiet(fn, *a, **k):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            r = fn(*a, **k)
        except SystemExit as e:
            return ("EXIT", e.code, buf.getvalue())
    return r if r is not None else buf.getvalue()


class FakeDrive:
    """Server ids are assigned per folder identity (inode stands in for the server object) and per file path (DriveFS
    keeps a file's id across atomic renames). ack() reports what the 'server' holds: the bytes present at ack time."""

    def __init__(self):
        self.folder_ids, self.file_ids = {}, {}
        self.readable = True
        self.server_down = False
        self.on_ack = None
        self.acks = 0

    def item_id(self, path):
        p = Path(path)
        if not self.readable or not p.exists():
            return None
        key = (str(p), os.stat(p).st_ino)
        return self.folder_ids.setdefault(key, "F" + hashlib.sha1(repr(key).encode()).hexdigest()[:20])

    def wait_cloud_ack(self, root_item_id, root_path, files, timeout=0, interval=0, scratch=None, base=None):
        self.acks += 1
        if self.on_ack:
            self.on_ack(self.acks)
        root_path = Path(root_path)
        if self.server_down:
            return {"status": "UNACKED", "files": {}, "source": "fake", "elapsed_s": 0}
        out = {}
        for f in files:
            rel = Path(f).relative_to(root_path).as_posix()
            fid = self.file_ids.setdefault(rel, "D" + hashlib.sha1(rel.encode()).hexdigest()[:24])
            out[rel] = {"status": "ACK", "drive_id": fid} if Path(f).is_file() else {"status": "UNACKED", "reason": "NOT_FOUND"}
        st = "ACK" if all(v["status"] == "ACK" for v in out.values()) else "UNACKED"
        return {"status": st, "files": out, "source": "fake", "elapsed_s": 0}


class DriveMirrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="drive-test-"))
        shutil.rmtree(core.ROOT, ignore_errors=True)
        quiet(cli.main, ["init", str(core.ROOT)])
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.invalid")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "a.txt").write_text("a")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "one")
        quiet(cli.main, ["register", "dummy", "--name", "Dummy", "--repo", str(self.repo)])
        self.p = core.project_dir("dummy")
        state, _ = quiet(sessions.begin, "dummy", "claude", "seed")
        (self.p / "CURRENT.md").write_text("# Dummy CURRENT\nSTATUS=SAFE\n")
        (self.p / "NEXT.md").write_text("# Dummy NEXT\n- read only\n")
        quiet(sessions.finish, "dummy", session=state["id"], result="PASS", label="seed")
        self.cs = self.tmp / "CloudStorage"
        self.md = self.cs / "GoogleDrive-test@example.invalid" / "My Drive"
        self.droot = self.md / "AI-Memory"
        self.droot.mkdir(parents=True)
        self.fake = FakeDrive()
        self.patches = [mock.patch.dict(os.environ, {"AIMEM_CLOUDSTORAGE_DIR": str(self.cs)}),
                        mock.patch.object(driveid, "item_id", self.fake.item_id),
                        mock.patch.object(driveid, "wait_cloud_ack", self.fake.wait_cloud_ack)]
        for pt in self.patches:
            pt.start()
        drive.pin()
        drive.register("dummy")
        self.pdir = self.droot / "dummy"

    def tearDown(self):
        for pt in reversed(self.patches):
            pt.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def canon(self):
        return {n: core.sha256_file(self.p / n) for n in ("CURRENT.md", "NEXT.md", "project.json")}

    def manifest(self):
        return json.loads((self.pdir / "MANIFEST.json").read_text())

    def edit_and_finish(self, text):
        state, _ = quiet(sessions.begin, "dummy", "codex", "edit")
        (self.p / "CURRENT.md").write_text(text)
        return quiet(sessions.finish, "dummy", session=state["id"], result="PASS", label="edit")

    # ---------------------------------------------------------------- publish protocol

    def test_push_commits_verified_complete_version(self):
        before = self.canon()
        r = drive.push("dummy", agent="claude", session="S1")
        self.assertEqual(r["status"], "VERIFIED")
        self.assertEqual(self.canon(), before, "local canonical memory must be untouched")
        m = self.manifest()
        self.assertEqual((m["version"], m["parent_version"], m["status"]), (1, 0, "COMMITTED"))
        for n in ("CURRENT.md", "NEXT.md", "LATEST_HANDOFF.md", "DECISIONS.jsonl", "EVENTS.jsonl", "LATEST_CHECKPOINT.json",
                  "MANIFEST.json"):
            self.assertTrue((self.pdir / n).is_file(), n)
        for d in drive.SUBDIRS:
            self.assertTrue((self.pdir / d).is_dir(), d)
        for rel, ent in m["files"].items():
            self.assertEqual(core.sha256_file(self.pdir / rel), ent["sha256"], rel)
            self.assertTrue(ent["drive_id"].startswith("D"))
        self.assertEqual(m["source"]["current_sha256"], core.sha256_file(self.p / "CURRENT.md"))
        hand = (self.pdir / "LATEST_HANDOFF.md").read_text()
        self.assertIn(f"DRIVE_VERSION: 1\nGENERATION: {m['generation']}", hand)
        for n in drive.HOT:
            self.assertEqual(core.sha256_file(self.pdir / m["current_version_dir"] / n), m["files"][n]["sha256"])
        self.assertTrue((self.pdir / "PROMPTS" / "DRIVE_AGENT_PROTOCOL.md").is_file())
        self.assertTrue((self.pdir / "CHECKPOINTS" / m["source"]["current_checkpoint"] / "meta.json").is_file())
        self.assertIn("identity_scheme", json.loads(drive.reg_path().read_text()))
        self.assertNotIn("device", drive.reg_path().read_text())
        v = drive.verify("dummy")
        for k in ("STABLE_DRIVE_IDENTITY", "MANIFEST", "MOUNT_READBACK", "REMOTE_READBACK", "DRIVE_IDS_STABLE",
                  "VERSION_DIR_COMPLETE", "SECRET_SCAN"):
            self.assertEqual(v[k], "PASS", k)
        self.assertEqual(v["LOCAL_MATCH"], "YES")
        self.assertTrue((self.droot / "README_AGENTS.md").is_file())
        self.assertEqual(json.loads((self.droot / "PROJECTS.json").read_text())["projects"]["dummy"]["version"], 1)

    def test_up_to_date_then_new_version_links_parent(self):
        drive.push("dummy")
        self.assertEqual(drive.push("dummy")["status"], "UP_TO_DATE")
        m1 = self.manifest()
        self.edit_and_finish("# Dummy CURRENT\nSTATUS=V2\n")  # finish hook publishes v2
        m2 = self.manifest()
        self.assertEqual((m2["version"], m2["parent_version"]), (2, 1))
        self.assertEqual(m2["parent_manifest_sha256"], hashlib.sha256(json.dumps(m1, ensure_ascii=False, indent=2,
                                                                                 sort_keys=True).encode() + b"\n").hexdigest())
        self.assertEqual(m2["files"]["CURRENT.md"]["drive_id"], m1["files"]["CURRENT.md"]["drive_id"], "stable file id")
        self.assertTrue((self.pdir / m1["current_version_dir"] / "CURRENT.md").is_file(), "old versions stay immutable")

    def test_unacked_content_never_becomes_current(self):
        drive.push("dummy")
        m1 = (self.pdir / "MANIFEST.json").read_bytes()
        (self.p / "CURRENT.md").write_text("# Dummy CURRENT\nSTATUS=LOCAL_EDIT\n")
        self.fake.server_down = True
        r = drive.push("dummy")
        self.assertEqual(r["status"], "PENDING_CLOUD_ACK")
        self.assertEqual((self.pdir / "MANIFEST.json").read_bytes(), m1, "previous version stays current")
        self.assertTrue(drive.pending_path("dummy").exists())
        self.assertFalse(drive.lease_live(drive.read_lease(self.pdir)), "lease released")
        self.fake.server_down = False
        self.assertEqual(drive.sweep_retry(), ["dummy:VERIFIED"])
        self.assertEqual(self.manifest()["version"], 2)
        self.assertFalse(drive.pending_path("dummy").exists())

    def test_live_lease_blocks_second_writer_and_stale_lease_is_taken_over(self):
        drive.push("dummy")
        (self.p / "CURRENT.md").write_text("# changed\n")
        lease = {"schema": drive.SCHEMA, "state": "HELD", "lease_id": "x", "holder": {"agent": "codex", "session": "C1",
                 "root_id": "other"}, "expires_at": "2999-01-01T00:00:00+00:00"}
        (self.pdir / "LEASE.json").write_text(json.dumps(lease))
        before = (self.pdir / "MANIFEST.json").read_bytes()
        with self.assertRaises(drive.DriveError) as c:
            drive.push("dummy", agent="claude", session="S2")
        self.assertEqual((c.exception.code, c.exception.exit_code), ("LEASE_HELD", core.EXIT_CONFLICT))
        self.assertEqual((self.pdir / "MANIFEST.json").read_bytes(), before)
        lease["expires_at"] = "2000-01-01T00:00:00+00:00"
        (self.pdir / "LEASE.json").write_text(json.dumps(lease))
        r = drive.push("dummy", agent="claude", session="S2")
        self.assertEqual(r["status"], "VERIFIED")
        self.assertTrue(any(n.startswith("STALE_WRITER_LEASE_TAKEN_OVER") for n in r["notes"]))
        self.assertEqual(drive.read_lease(self.pdir)["state"], "RELEASED")

    def test_stale_writer_cas_aborts_commit(self):
        drive.push("dummy")
        (self.p / "CURRENT.md").write_text("# changed again\n")
        m = self.manifest()

        def intruder(n):  # another writer commits while we wait for the server
            if n == self.fake.acks:
                m2 = dict(m, version=m["version"] + 1, generation="intruder")
                (self.pdir / "MANIFEST.json").write_text(json.dumps(m2))
        self.fake.on_ack = intruder
        with self.assertRaises(drive.DriveError) as c:
            drive.push("dummy")
        self.assertEqual(c.exception.code, "STALE_WRITER")
        self.assertEqual(self.manifest()["generation"], "intruder", "the other writer's commit is never overwritten")

    def test_conflicts_never_overwrite(self):
        drive.push("dummy")
        good = self.manifest()
        cases = [
            ("REMOTE_NEWER", dict(good, version=5, source=dict(good["source"], memory_version=10_000))),
            ("REMOTE_ROLLED_BACK", dict(good, version=0)),
            ("REMOTE_REWRITTEN", dict(good, generation="edited-by-hand")),
            ("CONFLICT_FOREIGN_WRITER", dict(good, version=3, writer=dict(good["writer"], root_id="another-mac"))),
        ]
        (self.p / "CURRENT.md").write_text("# newer local\n")
        for code, bad in cases:
            (self.pdir / "MANIFEST.json").write_text(json.dumps(bad))
            raw = (self.pdir / "MANIFEST.json").read_bytes()
            with self.assertRaises(drive.DriveError) as c:
                drive.push("dummy")
            self.assertEqual((c.exception.code, c.exception.exit_code), (code, core.EXIT_CONFLICT))
            self.assertEqual((self.pdir / "MANIFEST.json").read_bytes(), raw, code)
        (self.pdir / "MANIFEST.json").write_text(json.dumps(cases[2][1]))
        rc = drive.reconcile_cmd("dummy", apply=True)
        self.assertEqual(rc["remote_conflict"], "REMOTE_REWRITTEN")
        self.assertTrue(rc["recommended_action"].startswith("MANUAL_RECONCILIATION"))
        self.assertNotIn("applied", rc)
        (self.pdir / "MANIFEST.json").write_text("{not json")
        with self.assertRaises(drive.DriveError) as c:
            drive.push("dummy")
        self.assertEqual(c.exception.code, "REMOTE_MANIFEST_MALFORMED")
        (self.pdir / "MANIFEST.json").unlink()
        with self.assertRaises(drive.DriveError) as c:
            drive.push("dummy")
        self.assertEqual(c.exception.code, "REMOTE_MANIFEST_MISSING")

    def test_out_of_protocol_edit_is_quarantined_not_lost(self):
        drive.push("dummy")
        (self.pdir / "NEXT.md").write_text("edited in the browser\n")
        (self.p / "CURRENT.md").write_text("# local v2\n")
        r = drive.push("dummy")
        self.assertEqual(r["status"], "VERIFIED")
        q = [n for n in drive.inbox_items(self.pdir) if n.startswith("QUARANTINE_") and n.endswith("NEXT.md")]
        self.assertEqual(len(q), 1)
        self.assertEqual((self.pdir / "INBOX" / q[0]).read_text(), "edited in the browser\n")

    def test_secret_refused_nothing_published(self):
        (self.p / "NEXT.md").write_text("login otp is 123456\n")
        with self.assertRaises(drive.DriveError) as c:
            drive.push("dummy")
        self.assertEqual(c.exception.code, "REFUSED_SECRET")
        self.assertNotIn("123456", c.exception.detail)
        self.assertFalse((self.pdir / "MANIFEST.json").exists())
        self.assertFalse((self.pdir / "NEXT.md").exists())
        for s in ("IBAN GB82 WEST 1234 5698 7654 32", "card 4111 1111 1111 1111", "https://u:hunter2x" + chr(64) + "h.example/x",
                  "security answer: Fluffy2019dog", "otpauth://totp/x"):
            self.assertTrue(drive.secret_hits(s), s)
        for s in ("sha256 4111111111111111ab", "API key: xcodebuild exportArchive", "ts 1790659120330", "STATUS=PASS"):
            self.assertFalse(drive.secret_hits(s), s)

    # ---------------------------------------------------------------- identity

    def test_identity_is_drive_ids_not_device_numbers(self):
        drive.push("dummy")
        self.assertEqual(drive.status("dummy")["identity"], "PASS")
        src = (Path(drive.__file__).parent / "driveid.py").read_text() + Path(drive.__file__).read_text()
        self.assertNotIn(".st_dev", src)
        self.assertNotIn(".st_ino", src)
        moved = self.droot.with_name("AI-Memory-old")
        self.droot.rename(moved)
        self.droot.mkdir()  # same name, different Drive folder
        with self.assertRaises(drive.DriveError) as c:
            drive.status("dummy")
        self.assertEqual(c.exception.code, "DRIVE_IDENTITY_MISMATCH")
        self.droot.rmdir()
        moved.rename(self.droot)
        self.fake.readable = False
        with self.assertRaises(drive.DriveError) as c:
            drive.status("dummy")
        self.assertEqual(c.exception.code, "DRIVE_IDENTITY_UNVERIFIABLE")
        self.fake.readable = True
        self.droot.rename(moved)
        self.droot.mkdir()
        with self.assertRaises(drive.DriveError) as c:
            drive.pin()
        self.assertEqual(c.exception.code, "DRIVE_ROOT_ID_CHANGED")

    def test_proto_md5_parser(self):
        md5 = hashlib.md5(b"x").hexdigest().encode()
        buf = b"\x0a\x211ExampleDriveFileIdForUnitTests00" + b"\x10\x05" + b"\x3a\x20" + md5
        self.assertEqual(driveid.proto_md5s(buf), {md5.decode()})
        self.assertEqual(driveid.proto_md5s(b"\x0a\xff"), set())
        self.assertFalse(driveid.valid_item_id("local-12345678901"))

    # ---------------------------------------------------------------- read side + hooks

    def test_pull_is_read_only_and_falls_back_to_version_dir(self):
        drive.push("dummy")
        before = self.canon()
        (self.pdir / "INBOX" / "20260929T000000Z__chatgpt__note.md").write_text("proposal: verified tests PASS\n")
        (self.pdir / "CURRENT.md").write_text("half-written by a concurrent publish\n")
        r = drive.pull("dummy")
        self.assertEqual(r["sources"]["CURRENT.md"], self.manifest()["current_version_dir"])
        self.assertEqual(core.sha256_file(Path(r["dest"]) / "CURRENT.md"), before["CURRENT.md"])
        self.assertEqual(r["inbox"][0]["status"], "COPIED_FOR_REVIEW")
        self.assertEqual(self.canon(), before)
        self.assertTrue(str(Path(r["dest"])).startswith(str(core.ROOT / ".drive")))

    def test_begin_and_finish_hooks(self):
        drive.push("dummy")
        state, out = quiet(sessions.begin, "dummy", "claude", "hooks")
        self.assertTrue(any(l.startswith("DRIVE_STATE=MATCH DRIVE_VERSION=1") for l in out), out)
        (self.p / "CURRENT.md").write_text("# after hooks\n")
        printed = quiet(sessions.finish, "dummy", session=state["id"], result="PASS", label="hooks")
        self.assertEqual(self.manifest()["version"], 2)
        self.assertEqual(self.manifest()["writer"]["session"], state["id"])
        self.assertEqual(self.manifest()["writer"]["agent"], "claude")
        self.assertEqual(drive.reconcile_cmd("dummy")["local_vs_drive"], "MATCH")
        # a conflict at finish never undoes the local checkpoint
        m = self.manifest()
        (self.pdir / "MANIFEST.json").write_text(json.dumps(dict(m, generation="tampered")))
        state, _ = quiet(sessions.begin, "dummy", "claude", "after tamper")
        (self.p / "CURRENT.md").write_text("# local wins locally\n")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cp, ver = sessions.finish("dummy", session=state["id"], result="PASS", label="tamper")
        self.assertIn("DRIVE_SYNC=CONFLICT REASON=REMOTE_REWRITTEN", buf.getvalue())
        self.assertEqual(core.project_manifest("dummy")["current_checkpoint"], cp)
        self.assertEqual(self.manifest()["generation"], "tampered")
        _ = printed

    def test_unregistered_project_is_noop(self):
        quiet(cli.main, ["register", "other", "--name", "Other"])
        state, out = quiet(sessions.begin, "other", "claude", "x")
        (core.project_dir("other") / "CURRENT.md").write_text("# other\n")
        self.assertFalse(any(l.startswith("DRIVE_") for l in out))
        printed = quiet(sessions.finish, "other", session=state["id"], result="PASS", label="x")
        self.assertNotIn("DRIVE_SYNC", str(printed))


if __name__ == "__main__":
    unittest.main()
