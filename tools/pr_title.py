"""Checks a PR title against Conventional Commits and sets the matching labels.

The release notes are grouped by label (.github/release.yml), so deriving the
label from the title means nobody has to remember to set one.

    python tools/pr_title.py "feat: vertical export"         # check, print labels
    python tools/pr_title.py --apply <pr-number>             # CI: also edit the PR

In CI the title, branches and number come from the environment (TITLE,
HEAD_REF, BASE_REF), never from text pasted into the script, because a PR
title is untrusted input.
Exit code: 0 valid, 1 invalid title, 2 usage or gh error.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

TITLE_RE = re.compile(
    r"^(?P<type>feat|fix|docs|test|ci|chore|refactor|perf|build|revert)"
    r"(?:\((?P<scope>[a-z0-9-]+)\))?(?P<bang>!)?: \S.*$"
)
TYPE_LABEL = {
    "feat": "feature", "perf": "feature",
    "fix": "bug",
    "docs": "docs",
    "ci": "ci", "build": "ci",
    "test": "chore", "chore": "chore", "refactor": "chore", "revert": "chore",
}
# Labels this tool owns: it adds and removes them. Others are left alone.
MANAGED = set(TYPE_LABEL.values()) | {"breaking", "skip-changelog"}


def labels_for(title: str, head_ref: str = "", base_ref: str = "") -> set[str] | None:
    """Labels for a valid title, or None when the title doesn't follow the rules."""
    m = TITLE_RE.match(title.strip())
    if not m:
        return None
    labels = {TYPE_LABEL[m["type"]]}
    if m["bang"]:
        labels.add("breaking")
    # Release PRs, version bumps and hotfix syncs aren't changes of their own:
    # what they carry is already listed through the PRs that made it.
    if m["scope"] == "release" or (head_ref, base_ref) in {("develop", "main"), ("main", "develop")}:
        labels.add("skip-changelog")
    return labels


def apply(pr: str, wanted: set[str]) -> None:
    current = json.loads(subprocess.run(
        ["gh", "pr", "view", pr, "--json", "labels"], capture_output=True, text=True, check=True,
    ).stdout)
    have = {lab["name"] for lab in current["labels"]}
    add = sorted(wanted - have)
    remove = sorted((have & MANAGED) - wanted)
    if add or remove:
        cmd = ["gh", "pr", "edit", pr]
        for lab in add:
            cmd += ["--add-label", lab]
        for lab in remove:
            cmd += ["--remove-label", lab]
        subprocess.run(cmd, check=True)
    print(f"labels: +{add or '[]'} -{remove or '[]'}")


def main() -> int:
    args = sys.argv[1:]
    pr = None
    if args[:1] == ["--apply"] and len(args) == 2:
        pr = args[1]
        title = os.environ.get("TITLE", "")
    elif len(args) == 1:
        title = args[0]
    else:
        print(__doc__)
        return 2

    labels = labels_for(title, os.environ.get("HEAD_REF", ""), os.environ.get("BASE_REF", ""))
    if labels is None:
        print(f"PR title doesn't follow Conventional Commits: {title!r}")
        print("Use '<type>: <what changed>', e.g. 'feat: vertical 9:16 export' or 'fix: music fade'.")
        print(f"Types: {', '.join(TYPE_LABEL)}. Add '!' before ':' for a breaking change.")
        print("See docs/development.md#pr-titles")
        return 1
    print(f"PR title OK → {', '.join(sorted(labels))}")
    if pr:
        try:
            apply(pr, labels)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            print(f"Couldn't set labels ({e}). Set them by hand.", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
