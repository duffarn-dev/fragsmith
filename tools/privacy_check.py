"""Stops personal data and secrets from reaching the public repo.

Checks commit identities (only GitHub noreply addresses are allowed) and file
content (real SteamID64s, private IPs, Windows user paths, email addresses,
tokens, private keys, webhook URLs).

    python tools/privacy_check.py --staged          # pre-commit hook (.githooks/)
    python tools/privacy_check.py --range A..B      # CI on a pull request
    python tools/privacy_check.py --tree            # every tracked file + every commit
    python tools/privacy_check.py --history         # every file version ever (audit)

A line containing "privacy-check: ignore" is skipped (documented examples).
An optional, gitignored `.privacy-local` file adds your own patterns, one regex
per line (your name, PC name, ...). It's local only: listing those strings in
the repo would publish exactly what the check protects.

Findings are printed masked, because CI logs are public too.
Exit code: 0 clean, 1 findings, 2 usage or git error.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATTERNS = ROOT / ".privacy-local"
IGNORE_MARKER = "privacy-check: ignore"

NOREPLY_SUFFIX = "@users.noreply.github.com"
NOREPLY_EXACT = {"noreply@github.com"}  # GitHub as committer of web merges

# Email domains that are fine in file content.
OK_EMAIL_DOMAINS = (
    "users.noreply.github.com", "github.com", "anthropic.com",
    "example.com", "example.org", "example.net",
)
# Placeholder names in Windows paths (docs and tests).
OK_PATH_USERS = {"me", "you", "user", "username", "name", "public", "default", "runneradmin"}

PATTERNS: dict[str, re.Pattern[str]] = {
    "steamid64": re.compile(r"(?<!\d)7656119\d{10}(?!\d)"),
    "private ip": re.compile(
        r"(?<![\d.])(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}(?![\d.])"),
    "user path": re.compile(r"(?i)\b[a-z]:[\\/]{1,2}users[\\/]{1,2}([^\\/\s\"'<>%`$*]+)"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})"),
    "github token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,})"),
    "anthropic key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile("-----BEGIN [A-Z ]*" + "PRIVATE KEY-----"),
    "discord webhook": re.compile(r"discord(?:app)?\.com/api/webhooks/\d+/[\w-]+"),
}
# Placeholder SteamIDs used in tests and docs: 76561190000000000-76561190000000999.
OK_STEAMID = re.compile(r"76561190000000\d{3}")


def git(*args: str) -> str:
    proc = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                          text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def load_local_patterns() -> dict[str, re.Pattern[str]]:
    if not LOCAL_PATTERNS.exists():
        return {}
    out = {}
    for n, line in enumerate(LOCAL_PATTERNS.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            out[f"local pattern {n}"] = re.compile(line, re.IGNORECASE)
    return out


def mask(text: str) -> str:
    return text[:4] + "…" if len(text) > 4 else "…"


def scan_line(line: str, patterns: dict[str, re.Pattern[str]]) -> list[tuple[str, str]]:
    """Returns (kind, masked match) for every problem in one line."""
    if IGNORE_MARKER in line:
        return []
    hits = []
    for kind, rx in patterns.items():
        for m in rx.finditer(line):
            if kind == "steamid64" and OK_STEAMID.fullmatch(m.group(0)):
                continue
            if kind == "email" and m.group(1).lower().endswith(OK_EMAIL_DOMAINS):
                continue
            if kind == "user path" and m.group(1).lower() in OK_PATH_USERS:
                continue
            hits.append((kind, mask(m.group(0))))
    return hits


def scan_text(label: str, text: str, patterns, findings: list[str]) -> None:
    if "\x00" in text[:8000]:  # binary
        return
    for n, line in enumerate(text.splitlines(), 1):
        for kind, shown in scan_line(line, patterns):
            findings.append(f"{label}:{n}: [{kind}] {shown}")


def scan_added_lines(diff: str, patterns, findings: list[str]) -> None:
    """Scans only the lines a unified diff (-U0) adds."""
    path, line_no = "?", 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            path = row[6:] if row.startswith("+++ b/") else row[4:]
        elif row.startswith("@@"):
            m = re.search(r"\+(\d+)", row)
            line_no = int(m.group(1)) if m else 0
        elif row.startswith("+"):
            for kind, shown in scan_line(row[1:], patterns):
                findings.append(f"{path}:{line_no}: [{kind}] {shown}")
            line_no += 1


def email_ok(email: str) -> bool:
    email = email.strip().lower()
    return email.endswith(NOREPLY_SUFFIX) or email in NOREPLY_EXACT


def check_identities(rev_args: list[str], findings: list[str]) -> None:
    log = git("log", "--format=%h%x00%ae%x00%ce", *rev_args)
    for row in log.splitlines():
        sha, author, committer = row.split("\x00")
        for role, email in (("author", author), ("committer", committer)):
            if not email_ok(email):
                findings.append(f"commit {sha}: [{role} email] {mask(email)} is not a GitHub noreply address")


def check_own_identity(findings: list[str]) -> None:
    """The identity the next commit will get (respects GIT_AUTHOR_EMAIL etc.)."""
    for var in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"):
        m = re.search(r"<([^>]*)>", git("var", var))
        email = m.group(1) if m else ""
        if not email_ok(email):
            findings.append(f"git identity: [{var}] {mask(email)} is not a GitHub noreply address "
                            f"(git config user.email <id>+<login>{NOREPLY_SUFFIX})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Check for personal data and secrets before they go public.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="staged changes + your git identity (pre-commit)")
    mode.add_argument("--range", metavar="A..B", help="commits and added lines in a range (pull requests)")
    mode.add_argument("--tree", action="store_true", help="every tracked file + every commit identity")
    mode.add_argument("--history", action="store_true", help="every file version in every ref (slow audit)")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # the Windows console is cp1252

    patterns = {**PATTERNS, **load_local_patterns()}
    findings: list[str] = []
    try:
        if args.staged:
            check_own_identity(findings)
            scan_added_lines(git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff"), patterns, findings)
        elif args.range:
            check_identities([args.range], findings)
            base, _, head = args.range.partition("..")
            # three dots: diff from the merge base, so changes that landed on the
            # base branch meanwhile don't count as this branch's additions
            scan_added_lines(git("diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...{head}"),
                             patterns, findings)
        elif args.tree:
            check_identities(["HEAD"], findings)
            for path in git("ls-files", "-z").split("\x00"):
                if path and (ROOT / path).is_file():
                    text = (ROOT / path).read_text(encoding="utf-8", errors="replace")
                    scan_text(path, text, patterns, findings)
        else:
            check_identities(["--all"], findings)
            for row in git("rev-list", "--all", "--objects").splitlines():
                sha, _, path = row.partition(" ")
                if path and git("cat-file", "-t", sha).strip() == "blob":
                    scan_text(f"{path}@{sha[:7]}", git("cat-file", "-p", sha), patterns, findings)
    except RuntimeError as e:
        print(f"privacy check: {e}", file=sys.stderr)
        return 2

    if not findings:
        print("Privacy check: nothing personal or secret found.")
        return 0
    print(f"Privacy check: {len(findings)} problem(s). Matches are masked.")
    for f in dict.fromkeys(findings):
        print(f"  {f}")
    print(f"\nRemove them, or add '{IGNORE_MARKER}' to a line that is a deliberate example.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
