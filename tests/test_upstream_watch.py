"""Tests for tools/upstream_watch.py: version comparison, the sources, and which
issues get opened or closed. No network: every answer is faked.

Run: python tests/test_upstream_watch.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools import upstream_watch as uw  # noqa: E402

FAILS: list[str] = []

TESTED = {"cs2": "1.41.9.4", "hlae": "2.192.10", "cs_demo_manager": "3.20.1", "demoparser2": "0.42.1"}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def fake_fetch(answers: dict[str, object]):
    """fetch_json that answers by URL substring; raises for anything unknown."""
    def fetch(url: str, headers: dict) -> object:
        for part, answer in answers.items():
            if part in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise OSError(f"unexpected URL {url}")
    return fetch


def test_versions() -> None:
    print("version comparison")
    check("2.192.10 > 2.192.9", uw.is_newer("2.192.10", "2.192.9"))
    check("equal isn't newer", not uw.is_newer("3.20.1", "3.20.1"))
    check("older isn't newer", not uw.is_newer("0.41.3", "0.42.1"))
    check("4 parts (CS2)", uw.is_newer("1.41.10.0", "1.41.9.4"))
    check("leading v ignored", not uw.is_newer("v2.192.10", "2.192.10"))
    check("extra part is newer", uw.is_newer("3.20.1.1", "3.20.1"))


def test_sources() -> None:
    print("sources")
    fetch = fake_fetch({
        "UpToDateCheck": {"response": {"success": True, "message": "Server version required: 1.41.10.2"}},
        "GetNewsForApp": {"appnews": {"newsitems": [{"url": "https://example.com/patch"}]}},
        "advancedfx/releases/latest": {"tag_name": "v2.193.0", "html_url": "https://example.com/hlae"},
        "pypi.org/pypi/demoparser2": {"info": {"version": "0.43.0"}},
    })
    cs2 = uw.SOURCES["cs2"](fetch, {})
    check("CS2 version from Steam's message", cs2.version == "1.41.10.2", cs2.version)
    check("CS2 links the patch notes", cs2.url == "https://example.com/patch")
    hlae = uw.SOURCES["hlae"](fetch, {})
    check("GitHub tag without the v", (hlae.version, hlae.url) == ("2.193.0", "https://example.com/hlae"))
    check("PyPI version", uw.SOURCES["demoparser2"](fetch, {}).version == "0.43.0")

    no_news = fake_fetch({
        "UpToDateCheck": {"response": {"message": "Server version required: 1.41.10.2"}},
        "GetNewsForApp": OSError("down"),
    })
    check("CS2 still works when the news API is down",
          uw.SOURCES["cs2"](no_news, {}).url == "https://store.steampowered.com/news/app/730")
    for name, answers, key in [
        ("Steam answer without a version", {"UpToDateCheck": {"response": {"message": "oops"}}}, "cs2"),
        ("PyPI pre-release is skipped", {"pypi.org": {"info": {"version": "0.43.0rc1"}}}, "demoparser2"),
    ]:
        try:
            uw.SOURCES[key](fake_fetch(answers), {})
            check(name, False, "no error")
        except ValueError:
            check(name, True)


def test_plan() -> None:
    print("which issues to open and close")
    latest = {"cs2": uw.Latest("1.41.9.4", "u"), "hlae": uw.Latest("2.193.0", "https://example.com/hlae"),
              "cs_demo_manager": uw.Latest("3.20.1", "u"), "demoparser2": uw.Latest("0.42.1", "u")}

    actions = uw.plan(TESTED, latest, existing=[])
    check("one issue for the one newer tool", [a["title"] for a in actions] == ["Upstream: HLAE 2.193.0"],
          str([a["title"] for a in actions]))
    body = actions[0]["body"]
    check("body has tested and new version", "| Last tested with Fragsmith | 2.192.10 |" in body
          and "**2.193.0**" in body)
    check("body says how to test and record", "python main.py selftest" in body and "--write-tested" in body
          and "compat/tested-versions.json" in body)
    check("body links the release notes", "https://example.com/hlae" in body)

    existing = [{"number": 7, "title": "Upstream: HLAE 2.193.0", "state": "closed"}]
    check("no duplicate when the issue exists (even closed)", uw.plan(TESTED, latest, existing) == [])

    existing = [{"number": 5, "title": "Upstream: HLAE 2.192.11", "state": "open"},
                {"number": 3, "title": "Upstream: HLAE 2.192.10", "state": "closed"},
                {"number": 4, "title": "Upstream: CS2 1.41.9.4", "state": "open"}]
    actions = uw.plan(TESTED, latest, existing)
    check("closes only the open issue for the same tool", actions and actions[0]["close"] == [5],
          str(actions and actions[0]["close"]))

    check("a missing source is skipped", uw.plan(TESTED, {"hlae": latest["hlae"]}, []) != [])

    existing = [{"number": 8, "title": "Upstream: HLAE 2.192.10", "state": "open"},   # now tested
                {"number": 9, "title": "Upstream: HLAE 2.193.0", "state": "open"},    # not yet
                {"number": 6, "title": "Upstream: CS2 1.41.9.4", "state": "closed"},  # already closed
                {"number": 10, "title": "Upstream: demoparser2 0.42.0", "state": "open"}]
    check("closes open issues whose version is tested and released",
          sorted(uw.resolved(TESTED, existing)) == [8, 10], str(uw.resolved(TESTED, existing)))


def test_tested_file() -> None:
    print("compat/tested-versions.json")
    data = json.loads(uw.TESTED_FILE.read_text(encoding="utf-8"))
    missing = [t.key for t in uw.TOOLS if t.key not in data]
    check("has every watched tool", not missing, str(missing))
    from lib.version import __version__
    check("names a Fragsmith version", "fragsmith" in data and uw.version_key(data["fragsmith"]) <= uw.version_key(__version__))


if __name__ == "__main__":
    test_versions()
    test_sources()
    test_plan()
    test_tested_file()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All upstream watch tests OK")
