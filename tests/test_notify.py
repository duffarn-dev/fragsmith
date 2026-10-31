"""Tests for tools/notify.py: the Discord messages and how they're sent.
No network: posting is faked.

Run: python tests/test_notify.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import notify  # noqa: E402
from tools import upstream_watch as uw  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_messages() -> None:
    print("messages")
    m = notify.upstream_message("HLAE", "2.192.10", "2.193.0", "https://example.com/issues/12")
    check("upstream names tool, versions and issue",
          all(s in m for s in ("HLAE", "2.193.0", "2.192.10", "https://example.com/issues/12")))
    check("release", notify.release_message("v0.2.0", "U") == "🚀 **fragsmith v0.2.0 er ute!** U")

    m = notify.failed_message("tests", "develop", "push", "U", "fix: music fade\n\nlong body")
    check("failed push quotes the first line of the commit", "etter «fix: music fade»" in m and "long body" not in m, m)
    m = notify.failed_message("tests", "develop", "push", "U", "")
    check("failed push without a commit title", "etter" not in m and "`develop`" in m, m)
    m = notify.failed_message("tests", "develop", "push", "U", "x" * 300)
    check("long commit titles are cut", len(m) < 200, str(len(m)))
    check("weekly test failure explains itself",
          "ukentlige" in notify.failed_message("tests", "main", "schedule", "U"))
    check("watcher failure explains itself",
          "Versjonsvakten" in notify.failed_message("upstream-watch", "main", "schedule", "U"))
    check("unknown workflow keeps its name", "mystery" in notify.failed_message("mystery", "main", "push", "U"))
    demo = notify.demo_messages()
    check("demo: one of each, marked [test]", len(demo) == 4 and all(d.startswith("[test] ") for d in demo))


def test_payload() -> None:
    print("payload")
    p = notify.payload("hei @everyone")
    check("never pings anyone", p["allowed_mentions"] == {"parse": []})
    check("posts as Fragsmith", p["username"] == "Fragsmith")
    check("stays under Discord's 2000 characters", len(notify.payload("a" * 5000)["content"]) <= 2000)


def test_send() -> None:
    print("sending")
    sent: list[tuple[str, dict]] = []

    def fake_post(url: str, body: dict) -> None:
        sent.append((url, body))

    def boom(url: str, body: dict) -> None:
        raise OSError("Discord is down")

    old = os.environ.pop("DISCORD_WEBHOOK_URL", None)
    try:
        check("no webhook: skipped, not an error", notify.send("x", post=fake_post) is False and not sent)
        os.environ["DISCORD_WEBHOOK_URL"] = "https://example.com/hook"
        check("dry run doesn't post", notify.send("x", dry_run=True, post=fake_post) is False and not sent)
        check("posts with a webhook", notify.send("hei", post=fake_post) is True
              and sent == [("https://example.com/hook", notify.payload("hei"))])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):  # keep the ::warning:: out of CI annotations
            posted = notify.send("x", post=boom)
        check("a failed post is only a warning", posted is False and "::warning::" in out.getvalue())
    finally:
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        if old is not None:
            os.environ["DISCORD_WEBHOOK_URL"] = old


def test_watcher_hands_over_versions() -> None:
    print("upstream watcher → notification")
    tested = {"cs2": "1.41.9.4", "hlae": "2.192.10", "cs_demo_manager": "3.20.1", "demoparser2": "0.42.1"}
    actions = uw.plan(tested, {"hlae": uw.Latest("2.193.0", "u")}, [])
    check("the plan carries the tested and new version",
          actions and (actions[0]["tested"], actions[0]["new"]) == ("2.192.10", "2.193.0"))


if __name__ == "__main__":
    test_messages()
    test_payload()
    test_send()
    test_watcher_hands_over_versions()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All notify tests OK")
