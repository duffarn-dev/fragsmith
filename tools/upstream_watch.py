"""Watches the tools Fragsmith depends on and opens an issue when one updates.

Compares the newest CS2, HLAE, CS Demo Manager and demoparser2 versions with
the ones Fragsmith was last tested with (compat/tested-versions.json). For each
newer version it opens a GitHub issue labeled `upstream`, unless one with the
same title already exists (open or closed), and closes the older open issue for
the same tool, so there's at most one open issue per tool. Open issues whose
version has reached the tested file (on main: tested and released) are closed.

    python tools/upstream_watch.py --dry-run     # print what it would do
    python tools/upstream_watch.py               # CI: GITHUB_TOKEN + GITHUB_REPOSITORY

Standard library only. A source that's down is reported and skipped; the run
fails only if every source fails.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import notify  # noqa: E402  (tools/notify.py: Discord message for each new issue)

ROOT = Path(__file__).resolve().parent.parent
TESTED_FILE = ROOT / "compat" / "tested-versions.json"
API = "https://api.github.com"
LABEL = "upstream"

FetchJson = Callable[[str, dict], object]


@dataclass
class Tool:
    key: str            # key in tested-versions.json
    name: str           # human name, used in issue titles
    how_to_update: str  # first checklist item


@dataclass
class Latest:
    version: str
    url: str


TOOLS = [
    Tool("cs2", "CS2",
         "Let Steam update CS2 on the render PC (start Steam and wait until the update is done)."),
    Tool("hlae", "HLAE",
         "Update HLAE on the render PC: CS Demo Manager → Settings → Video → HLAE."),
    Tool("cs_demo_manager", "CS Demo Manager",
         "Install the new CS Demo Manager on the render PC (installer on the release page)."),
    Tool("demoparser2", "demoparser2",
         "`pip install -U demoparser2` in the venv on the PC that parses demos."),
]


def http_json(url: str, headers: dict) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "fragsmith-upstream-watch", **headers})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def version_key(v: str) -> tuple:
    """Numeric comparison, so 2.192.10 > 2.192.9. Non-numeric parts sort first."""
    return tuple((0, int(p)) if p.isdigit() else (-1, p) for p in re.split(r"[.\-+]", v.lstrip("v")))


def is_newer(latest: str, tested: str) -> bool:
    return version_key(latest) > version_key(tested)


def latest_cs2(fetch: FetchJson, gh_headers: dict) -> Latest:
    data = fetch("https://api.steampowered.com/ISteamApps/UpToDateCheck/v1/?appid=730&version=0", {})
    m = re.search(r"(\d+(?:\.\d+){2,})", data["response"].get("message", ""))
    if not m:
        raise ValueError(f"no version in Steam's answer: {data['response']}")
    url = "https://store.steampowered.com/news/app/730"
    try:
        news = fetch("https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
                     "?appid=730&count=1&feeds=steam_community_announcements", {})
        url = news["appnews"]["newsitems"][0]["url"] or url
    except Exception:  # patch notes are a nice-to-have
        pass
    return Latest(m.group(1), url)


def latest_github(repo: str) -> Callable[[FetchJson, dict], Latest]:
    def get(fetch: FetchJson, gh_headers: dict) -> Latest:
        # /releases/latest skips drafts and pre-releases
        data = fetch(f"{API}/repos/{repo}/releases/latest", gh_headers)
        return Latest(data["tag_name"].lstrip("v"), data["html_url"])
    return get


def latest_pypi(package: str) -> Callable[[FetchJson, dict], Latest]:
    def get(fetch: FetchJson, gh_headers: dict) -> Latest:
        version = fetch(f"https://pypi.org/pypi/{package}/json", {})["info"]["version"]
        if re.search(r"[a-z]", version):
            raise ValueError(f"newest {package} is a pre-release ({version})")
        return Latest(version, f"https://pypi.org/project/{package}/{version}/")
    return get


SOURCES = {
    "cs2": latest_cs2,
    "hlae": latest_github("advancedfx/advancedfx"),
    "cs_demo_manager": latest_github("akiver/cs-demo-manager"),
    "demoparser2": latest_pypi("demoparser2"),
}


def issue_title(tool: Tool, version: str) -> str:
    return f"Upstream: {tool.name} {version}"


def issue_body(tool: Tool, tested: str, latest: Latest) -> str:
    return "\n".join([
        f"A new version of **{tool.name}** is out.",
        "",
        "| | Version |",
        "|---|---|",
        f"| Last tested with Fragsmith | {tested} |",
        f"| New | **{latest.version}** |",
        "",
        f"Release notes: {latest.url}",
        "",
        "### Checklist",
        f"- [ ] {tool.how_to_update}",
        "- [ ] Run `python main.py selftest` with a fresh demo (renders and edits two clips, then compares versions).",
        "- [ ] It passes: run it with `--write-tested` and commit `compat/tested-versions.json` "
        "in a PR to `develop`. The watcher closes this issue once that reaches `main` in a release.",
        "- [ ] It's broken: follow [When an update breaks things]"
        "(https://github.com/duffarn-dev/fragsmith/blob/main/docs/compatibility.md#when-an-update-breaks-things).",
        "",
        "_Opened by the upstream watcher (`tools/upstream_watch.py`)._",
    ])


class GitHub:
    def __init__(self, repo: str, token: str, fetch: FetchJson = http_json):
        self.repo, self.fetch = repo, fetch
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

    def upstream_issues(self) -> list[dict]:
        issues, page = [], 1
        while True:
            batch = self.fetch(f"{API}/repos/{self.repo}/issues?state=all&labels={LABEL}"
                               f"&per_page=100&page={page}", self.headers)
            issues += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                return issues
            page += 1

    def _send(self, method: str, url: str, payload: dict) -> dict:
        req = urllib.request.Request(url, method=method, data=json.dumps(payload).encode(),
                                     headers={"User-Agent": "fragsmith-upstream-watch",
                                              "Content-Type": "application/json", **self.headers})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)

    def create_issue(self, title: str, body: str) -> dict:
        return self._send("POST", f"{API}/repos/{self.repo}/issues",
                          {"title": title, "body": body, "labels": [LABEL]})

    def close(self, number: int, comment: str, reason: str) -> None:
        self._send("POST", f"{API}/repos/{self.repo}/issues/{number}/comments", {"body": comment})
        self._send("PATCH", f"{API}/repos/{self.repo}/issues/{number}",
                   {"state": "closed", "state_reason": reason})


def plan(tested: dict, latest: dict[str, Latest], existing: list[dict]) -> list[dict]:
    """What to do: one entry per tool with a newer version and no issue yet."""
    titles = {i["title"] for i in existing}
    actions = []
    for tool in TOOLS:
        new = latest.get(tool.key)
        if new is None or not is_newer(new.version, tested[tool.key]):
            continue
        title = issue_title(tool, new.version)
        if title in titles:
            continue
        prefix = issue_title(tool, "")
        stale = [i["number"] for i in existing if i["state"] == "open" and i["title"].startswith(prefix)]
        actions.append({"tool": tool, "title": title, "body": issue_body(tool, tested[tool.key], new),
                        "close": stale, "tested": tested[tool.key], "new": new.version})
    return actions


def resolved(tested: dict, existing: list[dict]) -> list[int]:
    """Open issues whose version is now in the tested file, i.e. tested and released.

    Closing keywords ("Closes #12") only work for PRs into the default branch,
    and the tested-versions PR goes to develop, so the watcher closes these itself.
    """
    done = []
    for tool in TOOLS:
        prefix = issue_title(tool, "")
        for i in existing:
            if i["state"] == "open" and i["title"].startswith(prefix):
                version = i["title"][len(prefix):]
                if not is_newer(version, tested[tool.key]):
                    done.append(i["number"])
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    ap.add_argument("--tested", type=Path, default=TESTED_FILE, help="tested-versions file")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "duffarn-dev/fragsmith")
    gh_headers = {"Authorization": f"Bearer {token}"} if token else {}
    tested = json.loads(args.tested.read_text(encoding="utf-8"))

    latest: dict[str, Latest] = {}
    for tool in TOOLS:
        try:
            latest[tool.key] = SOURCES[tool.key](http_json, gh_headers)
        except Exception as e:
            print(f"::warning::{tool.name}: source unavailable ({e})")
            continue
        new = latest[tool.key].version
        mark = "NEW" if is_newer(new, tested[tool.key]) else "ok "
        print(f"  {mark}  {tool.name}: tested {tested[tool.key]}, newest {new}")
    if not latest:
        print("Every source failed.")
        return 1

    if not token:
        if not args.dry_run:
            print("GITHUB_TOKEN is not set: use --dry-run, or set it to open issues.")
            return 2
        existing = []
        print("(dry run without a token: existing issues not checked)")
    else:
        existing = GitHub(repo, token).upstream_issues()

    actions = plan(tested, latest, existing)
    done = resolved(tested, existing)
    if not actions and not done:
        print("Nothing new to report.")
        return 0
    gh = GitHub(repo, token)
    for number in done:
        if args.dry_run:
            print(f"would close #{number} (its version is in tested-versions.json)")
            continue
        gh.close(number, "Tested and released: this version is in `compat/tested-versions.json` on `main`.",
                 "completed")
        print(f"closed #{number} (tested and released)")
    for a in actions:
        if args.dry_run:
            print(f"would open: {a['title']}" + (f" (and close #{a['close']})" if a["close"] else ""))
            notify.send(notify.upstream_message(a["tool"].name, a["tested"], a["new"], "<issue>"), dry_run=True)
            continue
        issue = gh.create_issue(a["title"], a["body"])
        print(f"opened #{issue['number']}: {a['title']} {issue['html_url']}")
        notify.send(notify.upstream_message(a["tool"].name, a["tested"], a["new"], issue["html_url"]))
        for old in a["close"]:
            gh.close(old, f"Superseded by #{issue['number']}.", "not_planned")
            print(f"closed #{old} (superseded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
