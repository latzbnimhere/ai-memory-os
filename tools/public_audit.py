#!/usr/bin/env python3
"""Audit the tracked public tree for common privacy and secret leaks.

Owner-specific names can be supplied with repeatable --deny-term arguments at
runtime so they never need to be committed to the repository.
"""
from __future__ import annotations

import argparse
import ipaddress
import re
import subprocess
from pathlib import Path

FORBIDDEN_PARTS = {
    "projects",
    "sessions",
    "checkpoints",
    "artifacts",
    "logs",
    "reports",
    "baseline",
    "backups",
    ".previous",
    ".generated",
    ".handoff",
    ".run",
    ".locks",
    ".txn",
    "objects",
}

SECRET_PATTERNS = (
    ("OPENAI_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("ANTHROPIC_KEY", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("GITHUB_PAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("AWS_KEY", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GOOGLE_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b")),
    ("PRIVATE_KEY", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
)

EMAIL_RX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RX = re.compile(r"(?:\+971[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{4,7}|\b05\d{8}\b)")
ABS_PATH_RX = re.compile(
    r"(?:/" + "Users" + r"/[^/\s]+/|/" + "home" + r"/[^/\s]+/|[A-Z]:\\" + "Users" + r"\\[^\\\s]+\\)",
    re.I,
)
IPV4_RX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ALLOWED_EMAIL_DOMAINS = {"example.invalid", "example.com", "example.org", "users.noreply.github.com"}


def git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout


def tracked_files():
    return [Path(p) for p in git("ls-files").splitlines() if p]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Audit tracked public files")
    parser.add_argument("--deny-term", action="append", default=[], help="Additional private term to reject; repeatable")
    args = parser.parse_args(argv)

    hits = []
    files = tracked_files()

    if Path(".DO_NOT_PUBLISH") in files:
        hits.append(("TRACKED_PUBLICATION_MARKER", ".DO_NOT_PUBLISH", 0, ""))

    for line in git("ls-files", "-s").splitlines():
        if not line.strip():
            continue
        mode, _, _, path = line.split(maxsplit=3)
        if mode in {"120000", "160000"}:
            hits.append(("SYMLINK_OR_GITLINK", path, 0, mode))

    deny = [term.casefold() for term in args.deny_term if term]

    for path in files:
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            hits.append(("FORBIDDEN_TRACKED_PATH", str(path), 0, ""))

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            hits.append(("NON_UTF8_TRACKED_FILE", str(path), 0, ""))
            continue

        for number, line in enumerate(text.splitlines(), 1):
            folded = line.casefold()
            for term in deny:
                if term in folded:
                    hits.append(("DENY_TERM", str(path), number, line[:220]))

            if ABS_PATH_RX.search(line):
                hits.append(("PERSONAL_ABSOLUTE_PATH", str(path), number, line[:220]))

            if PHONE_RX.search(line):
                hits.append(("PHONE_LIKE", str(path), number, line[:220]))

            for email in EMAIL_RX.findall(line):
                domain = email.rsplit("@", 1)[-1].lower()
                if domain not in ALLOWED_EMAIL_DOMAINS:
                    hits.append(("UNEXPECTED_EMAIL", str(path), number, email))

            for label, pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    hits.append((label, str(path), number, line[:220]))

            for candidate in IPV4_RX.findall(line):
                try:
                    ip = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                if ip.is_global:
                    hits.append(("PUBLIC_IP", str(path), number, candidate))

    if hits:
        for hit in hits:
            print("\t".join(map(str, hit)))
        print(f"PUBLIC_AUDIT=STOP hits={len(hits)}")
        return 2

    print(f"PUBLIC_AUDIT=PASS files={len(files)}")
    print("TRACKED_RUNTIME_STATE=NONE")
    print("TRACKED_SYMLINKS_OR_GITLINKS=NONE")
    print("SECRET_PATTERNS=NONE")
    print("PERSONAL_ABSOLUTE_PATHS=NONE")
    print("UNEXPECTED_EMAILS=NONE")
    print("PUBLIC_IPS=NONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
