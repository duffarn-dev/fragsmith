# Releasing

Every merge to `main` is a release. The `release` workflow reads the version from `lib/version.py`, tags `main` with `vX.Y.Z` and publishes a GitHub Release with generated notes. There's nothing to tag or upload by hand.

## Version numbers

[Semantic Versioning](https://semver.org/). While Fragsmith is on 0.x:

| What changed since the last release | Bump | Example |
|---|---|---|
| a new feature or effect | minor | 0.3.0 → 0.4.0 |
| only fixes | patch | 0.3.0 → 0.3.1 |
| something breaking (a renamed config key, a changed CLI) | minor, and say so in the PR | 0.3.0 → 0.4.0 |

## Release day (Friday)

Release day is a maximum, not a deadline. If nothing finished is on `develop`, skip the release.

1. **`develop` is green** and holds nothing half-done.
2. **Check it with a real demo** on the render PC: `run` → `render` → `finish` on a fresh demo, and watch the film.
3. **Bump the version** with a PR to `develop`:
   ```
   git switch develop && git pull
   git switch -c chore/release-0.2.0
   # edit lib/version.py: __version__ = "0.2.0"
   git commit -am "chore(release): v0.2.0"
   git push -u origin chore/release-0.2.0
   gh pr create --base develop --fill
   ```
   Squash merge it once the checks are green.
4. **Release PR** from `develop` to `main`, titled `chore(release): v0.2.0`:
   ```
   gh pr create --base main --head develop --title "chore(release): v0.2.0" --body "Release v0.2.0"
   ```
   Merge it with a **merge commit** (the only method `main` allows).
5. **Check the release** on the Releases page: the tag, and notes grouped into Features / Fixes / Compatibility / Docs / Maintenance.

The `chore(release)` PRs get the `skip-changelog` label automatically, so the notes only list real changes.

## Hotfix

When the released version is broken (typically after a CS2 or HLAE update) and it can't wait for Friday:

1. `git switch main && git pull && git switch -c hotfix/short-name`
2. Fix it **and** bump the patch version in the same branch (`0.2.0` → `0.2.1`).
3. PR to `main` titled `fix: ...` → merge commit. The release workflow publishes v0.2.1.
4. PR from `main` to `develop` → **merge commit**, so `develop` has the fix and the new version.

## When something goes wrong

- **The workflow says "vX.Y.Z already exists".** `main` changed without a version bump. Nothing is published; bump the version and release again.
- **The notes put something under "Other".** The PR had no type label. Fix the label on the merged PR, then edit the notes on the Releases page or regenerate them with `gh release edit`.
- **Wrong release published.** Delete the release *and* its tag (`gh release delete vX.Y.Z --cleanup-tag`), fix, and merge again.
