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
| `develop` | The next version. Finished features wait here until release day. | Only via a PR, **squash** merged, so each feature becomes one commit. |
| `feature/…`, `fix/…`, `docs/…` | One branch per change, cut from `develop`. | Deleted automatically after merge. |
| `hotfix/…` | An urgent fix for the released version, cut from `main`. | PR to `main`, then `main` is merged back into `develop`. |

Both `main` and `develop` are protected. Nobody can push to them directly, force push or delete them, the maintainer included.

**Why two merge methods:** squashing keeps `develop` readable (one commit per PR, titled after the PR). A release must be a merge commit, because a squashed release would give `main` a commit that `develop` doesn't have, and every later release PR would show old changes again.

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

## Releases

Releases happen on a fixed day (Friday), but only when there's something finished on `develop`. If nothing is finished, there's no release. The release is a PR from `develop` to `main`, merged with a merge commit.

## Hotfixes

When the released version is broken and can't wait for release day (typically after a CS2 or HLAE update):

1. `git switch main && git pull && git switch -c hotfix/short-name`
2. Fix it, then open a PR to `main` and merge it with a merge commit.
3. Open a PR from `main` to `develop` so the fix isn't lost in the next release.

## Learnings

When something goes wrong in a non-obvious way, write it down in the same PR as the fix:

- traps for people changing the code (csdm quirks, ffmpeg gotchas) → [dev-notes.md](dev-notes.md)
- problems users can run into → [troubleshooting.md](troubleshooting.md)
