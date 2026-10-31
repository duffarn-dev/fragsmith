# Development workflow

How changes travel from an idea to a release. If you only want to send a fix, [CONTRIBUTING.md](../CONTRIBUTING.md) is enough. This page is the full picture.

## Branches

```
feature/x, fix/y ──squash──►  develop   (next version, tested on every PR)
                                 │
                                 │ release PR (merge commit)
                                 ▼
hotfix/z ──merge commit──────►  main     (released code, what users clone)
```

| Branch | What it is | How changes get in |
|---|---|---|
| `main` | The released version. The default branch, so a fresh clone is always the last release. | Only via a PR from `develop` (a release) or from `hotfix/…`, merged with a **merge commit**. |
| `develop` | The next version. Finished features wait here until release day. | Only via a PR. Feature PRs are **squash** merged, so each feature becomes one commit. A PR that brings a hotfix back from `main` is merged with a **merge commit**. |
| `feature/…`, `fix/…`, `docs/…` | One branch per change, cut from `develop`. | Deleted automatically after merge. |
| `hotfix/…` | An urgent fix for the released version, cut from `main`. | PR to `main`, then `main` is merged back into `develop`. |

Both `main` and `develop` are protected. Nobody can push to them directly, force push or delete them, the maintainer included.

**Why two merge methods:** squashing keeps `develop` readable (one commit per PR, titled after the PR). A release must be a merge commit, because a squashed release would give `main` a commit that `develop` doesn't have, and every later release PR would show old changes again.

## One-time setup per clone

```
git config core.hooksPath .githooks
git config user.email <id>+<login>@users.noreply.github.com
```

The hook runs `tools/privacy_check.py --staged` before every commit. It stops the commit if your git email isn't a GitHub noreply address, or if the staged lines contain a real Steam ID, a private IP, a `C:\Users\<name>` path, an email address, a token or a key. Your noreply address is listed at github.com/settings/emails.

For strings only you know are private (your name, your PC's name), put one regex per line in `.privacy-local`. The file is gitignored, and the hook and `--tree`/`--history` runs use it.

## Checks on every pull request

| Check | What it does |
|---|---|
| `tests` | All suites in `tests/` on Windows, with the oldest and newest supported Python. |
| `privacy` | `tools/privacy_check.py` on the commits and lines the PR adds. |

Both must pass before a PR can be merged into `develop` or `main`. Both also run every Monday against `main` with the newest dependency releases, so a breaking upstream release shows up even when nobody is working on the code.

CI runs **after** a push, when the branch is already public. The pre-commit hook is the check that actually keeps things private. CI is the safety net, and it covers contributors who don't have the hook.

## Day to day

```
git switch develop
git pull
git switch -c feature/vertical-export
# ...work, commit as often as you like...
python tests/test_render_modes.py        # and the other suites, see tests/README.md
git push -u origin feature/vertical-export
gh pr create --base develop --fill       # or open the PR on github.com
```

When the PR is merged, clean up locally:

```
git switch develop
git pull
git branch -d feature/vertical-export
```

## PR titles

PR titles follow [Conventional Commits](https://www.conventionalcommits.org/), because a squash merge turns the PR title into the commit message, and release notes are built from them:

| Prefix | For |
|---|---|
| `feat:` | a new feature or effect |
| `fix:` | a bug fix |
| `docs:` | documentation only |
| `test:` | tests only |
| `ci:` | GitHub Actions and repo automation |
| `chore:` | everything else (dependencies, cleanup) |

A breaking change, such as a renamed config key, gets a `!`: `feat!: rename [render].job_dir`. Describe the migration in the PR.

## Labels

| Label | For | Release notes |
|---|---|---|
| `feature` | a new feature or effect | Features |
| `bug` | something that doesn't work as documented | Fixes |
| `docs` | documentation only | Docs |
| `chore`, `ci` | cleanup, dependencies, automation | Maintenance |
| `upstream` | a new CS2, HLAE, CS Demo Manager or demoparser2 version (opened by the watcher) | Compatibility |
| `breaking` | changes config, CLI or output incompatibly | listed first |
| `skip-changelog` | left out of the release notes | — |
| `good first issue`, `help wanted` | invitations to contributors | — |

There are no `duplicate`, `wontfix` or `invalid` labels: close the issue as *duplicate* or *not planned* instead, which GitHub records itself.

New issues go through the forms in `.github/ISSUE_TEMPLATE/` (bug report, feature idea). Blank issues are off, and security problems go to the private Security tab.

## Releases

Releases happen on a fixed day (Friday), but only when there's something finished on `develop`. If nothing is finished, there's no release. The release is a PR from `develop` to `main`, merged with a merge commit.

## Hotfixes

When the released version is broken and can't wait for release day (typically after a CS2 or HLAE update):

1. `git switch main && git pull && git switch -c hotfix/short-name`
2. Fix it, then open a PR to `main` and merge it with a merge commit.
3. Open a PR from `main` to `develop` so the fix isn't lost in the next release. Merge it with a **merge commit**, not squash, so `develop` gets the same commit as `main` rather than a copy of it.

## Learnings

When something goes wrong in a non-obvious way, write it down in the same PR as the fix:

- traps for people changing the code (csdm quirks, ffmpeg gotchas) → [dev-notes.md](dev-notes.md)
- problems users can run into → [troubleshooting.md](troubleshooting.md)
