"""Tests for versioning and the release tooling: lib/version.py, --version,
the report header and tools/pr_title.py.

Run: python tests/test_release.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.version import __version__  # noqa: E402
from tools.pr_title import labels_for  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_version() -> None:
    print("version")
    check("SemVer", re.fullmatch(r"\d+\.\d+\.\d+", __version__) is not None, __version__)
    out = subprocess.run([sys.executable, str(ROOT / "main.py"), "--version"],
                         capture_output=True, text=True)
    check("main.py --version", out.returncode == 0 and out.stdout.strip() == f"fragsmith {__version__}",
          out.stdout.strip() or out.stderr.strip())
    # the release workflow reads the version with this regex (grep -oP)
    text = (ROOT / "lib" / "version.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    check("release workflow can read it", m is not None and m.group(1) == __version__)
    src = (ROOT / "lib" / "assemble.py").read_text(encoding="utf-8")
    check("report.md header names the version", "by Fragsmith {__version__}" in src)


def test_pr_titles() -> None:
    print("PR titles → labels")
    cases = [
        ("feat: vertical 9:16 export", "", "", {"feature"}),
        ("fix: music fade at film end", "", "", {"bug"}),
        ("docs: explain HLAE updates", "", "", {"docs"}),
        ("ci: bump the actions group", "", "", {"ci"}),
        ("chore: tidy imports", "", "", {"chore"}),
        ("test: cover drop sync", "", "", {"chore"}),
        ("perf: faster concat", "", "", {"feature"}),
        ("feat(render)!: rename [render].job_dir", "", "", {"feature", "breaking"}),
        ("chore(release): v0.2.0", "chore/release-0.2.0", "develop", {"chore", "skip-changelog"}),
        ("chore: release to main", "develop", "main", {"chore", "skip-changelog"}),
        ("fix: hotfix sync", "main", "develop", {"bug", "skip-changelog"}),
    ]
    for title, head, base, want in cases:
        got = labels_for(title, head, base)
        check(f"{title!r}", got == want, f"{sorted(got) if got else got}")
    for bad in ("Add vertical export", "feat:no space", "feature: x", "feat: ", "Feat: x", "fix(Scope): x"):
        check(f"rejects {bad!r}", labels_for(bad) is None)


if __name__ == "__main__":
    test_version()
    test_pr_titles()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All release tests OK")
