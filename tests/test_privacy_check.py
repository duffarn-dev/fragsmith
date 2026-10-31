"""Tests for tools/privacy_check.py: what counts as personal data or a secret.

Run: python tests/test_privacy_check.py

The fake secrets below are assembled at runtime, so this file doesn't trip the
check itself (or GitHub's push protection).
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import privacy_check as pc  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def kinds(line: str) -> list[str]:
    return [k for k, _ in pc.scan_line(line, pc.PATTERNS)]


def ip(*parts: int) -> str:
    return ".".join(map(str, parts))


def test_content_patterns() -> None:
    print("content patterns")
    real_id = "7656119" + "8012345678"
    check("real SteamID64 is caught", kinds(f"known = [{real_id}]") == ["steamid64"])
    check("placeholder SteamID64 passes", kinds("known = [76561190000000001]") == [])
    check("private IPs are caught",
          all(kinds(f"host = {a}") == ["private ip"]
              for a in (ip(192, 168, 1, 7), ip(10, 1, 2, 3), ip(172, 20, 0, 9))))
    check("version numbers aren't IPs",
          kinds("cs2 1.41.9.4, hlae 2.192.10, csdm 3.20.1, 10.0.0") == [])
    check("public IP isn't flagged by default", kinds("8.8.8.8") == [])
    check("user path is caught", kinds("key = 'C:\\Users\\" + "alice" + "\\.ssh\\id'") == ["user path"])
    check("placeholder user paths pass",
          kinds(r"C:\Users\<you>\.ssh and C:\Users\me\x and C:/Users/Public/x") == [])
    check("email is caught", kinds("contact: " + "alice" + "@" + "mail.com") == ["email"])
    check("noreply and example emails pass",
          kinds("1+x@users.noreply.github.com noreply@github.com a@example.com noreply@anthropic.com") == [])
    check("GitHub token is caught", kinds("token=" + "ghp_" + "a" * 36) == ["github token"])
    check("fine-grained token is caught", kinds("github_pat_" + "B" * 30) == ["github token"])
    check("Anthropic key is caught", kinds("ANTHROPIC_API_KEY=" + "sk-ant-" + "x" * 30) == ["anthropic key"])
    check("private key is caught", kinds("-----BEGIN OPENSSH " + "PRIVATE KEY-----") == ["private key"])
    check("Discord webhook is caught",
          kinds("https://discord.com/api/" + "webhooks/123456/abcDEF-123") == ["discord webhook"])
    check("ignore marker skips the line",
          kinds(f"host = {ip(192, 168, 1, 50)}  # {pc.IGNORE_MARKER}") == [])
    hits = pc.scan_line("token=" + "ghp_" + "a" * 36, pc.PATTERNS)
    check("matches are masked", hits and hits[0][1] == "ghp_…", str(hits))


def test_added_lines() -> None:
    print("diff scanning")
    diff = "\n".join([
        "diff --git a/x.toml b/x.toml",
        "--- a/x.toml",
        "+++ b/x.toml",
        "@@ -3,0 +4,2 @@",
        "+fine = 1",
        f"+host = {ip(192, 168, 0, 12)}",
        "@@ -9 +11 @@",
        f"-old = {ip(10, 0, 0, 1)}",
        "+new = 2",
    ])
    found: list[str] = []
    pc.scan_added_lines(diff, pc.PATTERNS, found)
    check("only added lines, with the right line number", found == ["x.toml:5: [private ip] 192.…"], str(found))


def test_identities() -> None:
    print("commit identities")
    check("noreply address passes", pc.email_ok("266322354+someone@users.noreply.github.com"))
    check("GitHub committer passes", pc.email_ok("noreply@github.com"))
    check("personal address fails", not pc.email_ok("someone@" + "mail.com"))

    with tempfile.TemporaryDirectory() as tmp:
        def git(*args: str, email: str) -> None:
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_COMMITTER_NAME": "t",
                   "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
            subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, env=env)

        git("init", "-q", email="x@users.noreply.github.com")
        Path(tmp, "a.txt").write_text("a\n")
        git("add", "a.txt", email="x@users.noreply.github.com")
        git("commit", "-q", "-m", "ok", email="x@users.noreply.github.com")
        Path(tmp, "a.txt").write_text("b\n")
        git("commit", "-q", "-am", "leak", email="someone@" + "mail.com")

        old_root = pc.ROOT
        pc.ROOT = Path(tmp)
        try:
            found: list[str] = []
            pc.check_identities(["HEAD"], found)
        finally:
            pc.ROOT = old_root
        check("leaking commit is flagged as author and committer",
              len(found) == 2 and all("some…" in f for f in found), str(found))


def test_local_patterns() -> None:
    print("local patterns")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp, ".privacy-local")
        f.write_text("# my stuff\n\nsecretname\n", encoding="utf-8")
        old = pc.LOCAL_PATTERNS
        pc.LOCAL_PATTERNS = f
        try:
            local = pc.load_local_patterns()
        finally:
            pc.LOCAL_PATTERNS = old
        check("comments and blank lines skipped", len(local) == 1, str(local))
        check("local pattern matches case-insensitively",
              [k for k, _ in pc.scan_line("Hi SecretName", local)] == ["local pattern 3"])


if __name__ == "__main__":
    test_content_patterns()
    test_added_lines()
    test_identities()
    test_local_patterns()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All privacy check tests OK")
