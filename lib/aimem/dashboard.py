"""Optional local dashboard: Python http.server bound to 127.0.0.1 only. No secrets shown."""
from __future__ import annotations

import html
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer

from . import backup, core, health, recover, sessions
from .core import RUN, config, project_dir

HOST = "127.0.0.1"  # fixed: never 0.0.0.0


def pid_file():
    return RUN / "dashboard.json"


def state_snapshot():
    h = health.collect(check_repo=True)
    projects = []
    for slug, v in h["per_project"].items():
        p = project_dir(slug)
        nxt = (p / "NEXT.md").read_text(errors="ignore") if (p / "NEXT.md").exists() else ""
        cur = (p / "CURRENT.md").read_text(errors="ignore") if (p / "CURRENT.md").exists() else ""
        status_line = next((l for l in cur.splitlines() if l.startswith("STATUS:")), "STATUS: -")
        next_action = ""
        grab = False
        for l in nxt.splitlines():
            if l.strip().lower().startswith("## next action"):
                grab = True
                continue
            if grab and l.startswith("## "):
                break
            if grab and l.strip():
                next_action += l.strip() + " "
        opens = []
        for f, j in sessions.open_sessions(slug):
            opens.append({"id": j.get("id"), "agent": j.get("agent"), "lease": sessions.lease_state(j),
                          "last_heartbeat": (j.get("lease") or {}).get("last_heartbeat"), "task": (j.get("task") or "")[:100]})
        recov = [a["id"] for a in recover.scan(slug) if a["recovery_required"]]
        errs = [e for e in core.read_jsonl(p / "EVENTS.jsonl", 200) if e.get("kind") in ("error",) or e.get("step_kind") == "error"][-3:]
        projects.append({"slug": slug, "status": core.redact(status_line.replace("STATUS:", "").strip(), 120), "checkpoint": v["checkpoint"],
                         "next_action": core.redact(next_action.strip()[:300], 300), "match": h["match_status"].get(slug, "-"),
                         "sessions": opens, "recovery": recov, "errors": [core.redact((e.get("summary") or e.get("text") or "")[:160], 160) for e in errs],
                         "checkpoints": v["checkpoints"], "memory_bytes": v["memory_bytes"], "context_estimate_tokens": v["context_estimate_tokens"],
                         "memory_version": v["memory_version"]})
    return {"generated": core.iso(), "health": {k: h[k] for k in ("AI_MEMORY_HEALTH", "version", "projects", "active_sessions", "stale_sessions",
                                                                 "abandoned_sessions", "recovery_required", "unresolved_transactions", "database",
                                                                 "backup_status", "backup_age_hours", "disk_usage", "launchagent")}, "projects": projects}


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>AI Memory OS — local dashboard</title>
<meta http-equiv="refresh" content="30">
<style>body{font-family:-apple-system,Helvetica,Arial,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}
h1{font-size:20px}table{border-collapse:collapse;width:100%;font-size:13px}td,th{border:1px solid #333;padding:6px 8px;vertical-align:top;text-align:left}
th{background:#1b1f27}.ok{color:#7ad47a}.warn{color:#f0c674}.fail{color:#ff6b6b}.mono{font-family:Menlo,monospace;font-size:12px}
.pill{display:inline-block;padding:1px 6px;border-radius:8px;background:#222;margin-right:4px}</style></head><body>
<h1>AI Memory OS — local dashboard <span class="mono">(127.0.0.1 only · no cloud · no API)</span></h1>
%HEALTH%
<table><tr><th>Project</th><th>Status</th><th>Checkpoint</th><th>Next action</th><th>Memory/physical</th><th>Sessions</th><th>Warnings</th><th>Size</th></tr>%ROWS%</table>
<p class="mono">generated %GEN% · refreshes every 30s · JSON: <a href="/api/state">/api/state</a></p></body></html>"""


def render_html(s):
    h = s["health"]
    cls = {"OK": "ok", "WARN": "warn", "FAIL": "fail"}.get(h["AI_MEMORY_HEALTH"], "warn")
    head = (f'<p><span class="pill {cls}">HEALTH {h["AI_MEMORY_HEALTH"]}</span> <span class="pill">v{h["version"]}</span> '
            f'<span class="pill">projects {h["projects"]}</span> <span class="pill">active {h["active_sessions"]}</span> '
            f'<span class="pill">stale {h["stale_sessions"]}</span> <span class="pill">abandoned {h["abandoned_sessions"]}</span> '
            f'<span class="pill">recovery {h["recovery_required"]}</span> <span class="pill">txn {h["unresolved_transactions"]}</span> '
            f'<span class="pill">db {h["database"]}</span> <span class="pill">backup {h["backup_status"]} ({h["backup_age_hours"]}h)</span> '
            f'<span class="pill">disk {h["disk_usage"]}</span> <span class="pill">launchd {h["launchagent"]}</span></p>')
    rows = []
    for p in s["projects"]:
        sess = "<br>".join(f'{html.escape(x["id"] or "")} <span class="pill">{x["lease"]}</span> hb {html.escape(str(x["last_heartbeat"]))}' for x in p["sessions"]) or "-"
        warns = []
        if p["recovery"]:
            warns.append('<span class="fail">RECOVERY_REQUIRED: ' + html.escape(", ".join(p["recovery"])) + "</span>")
        warns += [html.escape(e) for e in p["errors"]]
        mcls = "ok" if p["match"].startswith("MEMORY_MATCH") and "+" not in p["match"] else "warn"
        rows.append(f'<tr><td><b>{html.escape(p["slug"])}</b><br><span class="mono">v{p["memory_version"]} · {p["checkpoints"]} cps</span></td>'
                    f'<td>{html.escape(p["status"])}</td><td class="mono">{html.escape(str(p["checkpoint"]))}</td>'
                    f'<td>{html.escape(p["next_action"] or "-")}</td><td class="{mcls}">{html.escape(p["match"])}</td><td class="mono">{sess}</td>'
                    f'<td>{"<br>".join(warns) or "-"}</td><td class="mono">{core.human_bytes(p["memory_bytes"])}<br>~{p["context_estimate_tokens"]} tok</td></tr>')
    return PAGE.replace("%HEALTH%", head).replace("%ROWS%", "".join(rows)).replace("%GEN%", html.escape(s["generated"]))


class Handler(BaseHTTPRequestHandler):
    cache = {"t": 0, "s": None}
    lock = threading.Lock()

    def _snapshot(self):
        with Handler.lock:
            if time.time() - Handler.cache["t"] > 10 or Handler.cache["s"] is None:
                Handler.cache["s"] = state_snapshot()
                Handler.cache["t"] = time.time()
            return Handler.cache["s"]

    def do_GET(self):
        try:
            if self.path.startswith("/api/state"):
                body = json.dumps(self._snapshot(), indent=2).encode()
                ctype = "application/json"
            elif self.path in ("/", "/index.html"):
                body = render_html(self._snapshot()).encode()
                ctype = "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:  # never crash the server
            try:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())
            except Exception:
                pass

    def log_message(self, *a):
        pass


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    """HTTP server that binds locally without reverse-DNS lookup at startup."""

    def server_bind(self):
        # HTTPServer.server_bind() calls socket.getfqdn(host), which can block on
        # reverse DNS even for 127.0.0.1 in restricted/CI environments. Bind via
        # TCPServer directly, then set the two HTTPServer metadata attributes.
        TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


def serve(port):
    srv = LocalThreadingHTTPServer((HOST, port), Handler)
    RUN.mkdir(exist_ok=True)
    core.atomic_write_json(pid_file(), {"pid": os.getpid(), "host": HOST, "port": port, "started": core.iso()})
    try:
        srv.serve_forever()
    finally:
        try:
            pid_file().unlink()
        except OSError:
            pass


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def status():
    j = core.try_load_json(pid_file(), None)
    if not j or not _alive(int(j.get("pid", 0))):
        return {"running": False}
    j["running"] = True
    return j


def start(port=None, foreground=False):
    core.ensure_root()
    port = int(port or config().get("dashboard_port", 47119))
    st = status()
    if st.get("running"):
        return st
    if foreground:
        serve(port)
        return status()
    exe = Path(__file__).resolve().parents[2] / "bin" / "aimem"
    log = RUN / "dashboard.log"
    with log.open("a") as lf:
        subprocess.Popen([sys.executable, str(exe), "dashboard", "--serve", "--port", str(port)], stdout=lf, stderr=lf,
                         start_new_session=True, env=dict(os.environ, AI_MEMORY_ROOT=str(core.ROOT)))
    for _ in range(50):
        time.sleep(0.1)
        st = status()
        if st.get("running"):
            return st
    core.die("dashboard failed to start (see .run/dashboard.log)")


def stop():
    st = status()
    if not st.get("running"):
        return False
    os.kill(int(st["pid"]), signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if not _alive(int(st["pid"])):
            break
    try:
        pid_file().unlink()
    except OSError:
        pass
    return True
