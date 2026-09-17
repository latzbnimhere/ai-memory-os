# Local dashboard (optional)
`aimem dashboard` starts a detached `http.server` bound to `127.0.0.1` only (port from config `dashboard_port`, default 47119;
`--port` to override); `aimem dashboard --status` / `--stop`. Pages: `/` (HTML, auto-refresh 30 s) and `/api/state` (JSON).
Shows per project: status line, current checkpoint, next action, memory/physical match, open sessions with lease and last heartbeat,
recovery warnings, latest errors, checkpoint count, memory size, context estimate; global health incl. backup status. Text is passed
through the secret redactor; no secrets are displayed. The bind host is hard-coded; there is no option to expose it.
