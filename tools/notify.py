"""Posts short notifications to the maintainer's Discord channel.

The messages are in Norwegian: they're written for the maintainer, not for
the public repo. All the texts live in this file.

    python tools/notify.py release v0.2.0 https://github.com/.../releases/tag/v0.2.0
    python tools/notify.py failed <workflow> <branch> <event> <run-url>
    python tools/notify.py demo                       # one sample of each, marked [test]
    python tools/notify.py --dry-run demo             # print instead of posting

The webhook comes from DISCORD_WEBHOOK_URL (a repo secret). Without it, this
prints the message and exits 0, so forks and local runs don't fail. A failed
post is a warning, never a failed workflow. The commit title for `failed`
comes from COMMIT_TITLE in the environment, because it's untrusted input.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from typing import Callable

MAX_LEN = 1900  # Discord's limit is 2000 characters

WORKFLOW_NAMES = {
    "tests": "Testene",
    "privacy": "Personvernsjekken",
    "upstream-watch": "Versjonsvakten",
    "release": "Release-workflowen",
}


def upstream_message(tool: str, tested: str, new: str, issue_url: str) -> str:
    return (f"🔔 **Ny versjon av {tool}: {new}** (fragsmith er testet med {tested}).\n"
            f"Oppdater på render-PC-en og test en fersk demo (`run` → `render` → `finish`). "
            f"Sjekklista ligger i issuet: {issue_url}")


def release_message(tag: str, url: str) -> str:
    return f"🚀 **fragsmith {tag} er ute!** {url}"


def failed_message(workflow: str, branch: str, event: str, run_url: str, commit_title: str = "") -> str:
    name = WORKFLOW_NAMES.get(workflow, workflow)
    if event == "schedule":
        what = {
            "tests": "❌ **Den ukentlige testen av `main` feilet**, sannsynligvis på grunn av en ny pakkeversjon.",
            "upstream-watch": "❌ **Versjonsvakten feilet**, trolig fordi ingen av kildene svarte.",
        }.get(workflow, f"❌ **{name} feilet** i den planlagte kjøringen på `{branch}`.")
        return f"{what}\n{run_url}"
    title = commit_title.strip().splitlines()[0][:120] if commit_title.strip() else ""
    after = f" etter «{title}»" if title else ""
    return f"❌ **{name} feilet på `{branch}`**{after}.\n{run_url}"


def payload(text: str) -> dict:
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN - 1] + "…"
    # No pings, ever: commit titles are written by others and could contain @everyone.
    return {"username": "Fragsmith", "content": text, "allowed_mentions": {"parse": []}}


def post_discord(webhook: str, body: dict) -> None:
    req = urllib.request.Request(webhook, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "fragsmith-notify"})
    with urllib.request.urlopen(req, timeout=20):
        pass


def send(text: str, dry_run: bool = False, post: Callable[[str, dict], None] = post_discord) -> bool:
    """Posts one message. Returns True if it was posted."""
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
    if dry_run or not webhook:
        reason = "dry run" if dry_run else "DISCORD_WEBHOOK_URL is not set"
        print(f"({reason}, not posted)\n{text}")
        return False
    try:
        post(webhook, payload(text))
    except Exception as e:  # never let a notification fail the workflow
        print(f"::warning::Discord notification failed: {type(e).__name__}")
        return False
    print("Discord notification sent.")
    return True


def demo_messages() -> list[str]:
    run = "https://github.com/duffarn-dev/fragsmith/actions"
    return ["[test] " + m for m in (
        upstream_message("HLAE", "2.192.10", "2.193.0", "https://github.com/duffarn-dev/fragsmith/issues"),
        failed_message("tests", "develop", "push", run, "feat: an example change"),
        failed_message("tests", "main", "schedule", run),
        release_message("v0.0.0", "https://github.com/duffarn-dev/fragsmith/releases"),
    )]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    dry_run = "--dry-run" in argv
    args = [a for a in argv if a != "--dry-run"]
    if args[:1] == ["release"] and len(args) == 3:
        send(release_message(args[1], args[2]), dry_run)
    elif args[:1] == ["failed"] and len(args) == 5:
        send(failed_message(*args[1:], os.environ.get("COMMIT_TITLE", "")), dry_run)
    elif args == ["demo"]:
        for text in demo_messages():
            send(text, dry_run)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
