# Compatibility

Fragsmith sits on top of tools that update without asking: CS2 (often weekly), HLAE, CS Demo Manager and demoparser2. A CS2 update can break HLAE, and then nothing renders until HLAE catches up. This page covers how Fragsmith notices that, and what to do about it.

## Tested versions

[`compat/tested-versions.json`](../compat/tested-versions.json) lists the versions the current release was last tested with, end to end (parse → render → finished film). If your versions are newer, Fragsmith will most likely still work. If something breaks, those are the versions to compare against.

The file only changes through a PR, after a real test.

## The upstream watcher

Every day at 05:00 UTC, the `upstream-watch` workflow (`tools/upstream_watch.py`) asks:

| Tool | Source |
|---|---|
| CS2 | Steam's `UpToDateCheck` API (the same number as `PatchVersion` in `game/csgo/steam.inf`) |
| HLAE | the latest GitHub release of `advancedfx/advancedfx` |
| CS Demo Manager | the latest GitHub release of `akiver/cs-demo-manager` |
| demoparser2 | PyPI |

For every version newer than the tested one, it opens an issue titled `Upstream: <tool> <version>`, labeled `upstream`, with a checklist. It never opens the same title twice, even if the first one was closed. When a tool moves on again before anyone got to it, the older open issue is closed as superseded, so there's at most one open issue per tool. Once an issue's version is in `tested-versions.json` on `main` (tested and released), the watcher closes it as completed. Pre-releases are ignored.

Run it by hand:

```
python tools/upstream_watch.py --dry-run
```

or start the workflow on GitHub: **Actions → upstream-watch → Run workflow**.

GitHub pauses scheduled workflows in a repository with no activity for 60 days, and sends an email first. Any commit, or a manual run, starts it again.

## Handling an `upstream` issue

1. Update the tool on the render PC (the issue says how).
2. Render a fresh demo end to end: `run` → `render` → `finish`, and watch the film.
3. **It works:** in a PR to `develop`, set the new version in `compat/tested-versions.json` and link the issue. It ships with the next release, and the watcher then closes the issue itself. (`Closes #12` in a PR description only works for PRs into the default branch, and this one goes to `develop`.)
4. **It's broken:** see below.

## When an update breaks things

| Symptom | Likely cause | What to do |
|---|---|---|
| CS2 crashes or HLAE shows error dialogs right after a CS2 update | HLAE doesn't support the new CS2 build yet | Wait for an HLAE release (the watcher opens an issue for it), then update HLAE via CS Demo Manager → Settings → Video. Nothing to change in Fragsmith. |
| Parsing fails or events look wrong after a demoparser2 release | demoparser2 changed behavior | Hotfix: pin the last good version in `requirements.txt` (`demoparser2>=0.41.3,<X`) and release a patch (see [releasing.md](releasing.md#hotfix)). Then fix properly on `develop`. |
| `csdm video` fails or behaves differently after a CS Demo Manager release | the CLI changed | Stay on the previous installer on the render PC, and check the CS Demo Manager changelog. Adapt `worker.py` on `develop`. |
| "Demo file incompatible with this game version", or playback fails for one demo while fresh demos render fine | the demo is from an older CS2 version | Not a Fragsmith problem: get a fresh demo ([troubleshooting.md](troubleshooting.md#demo-file-incompatible-with-this-game-version)). |

When users could hit the problem, add it to [troubleshooting.md](troubleshooting.md). When it's a trap for developers, add it to [dev-notes.md](dev-notes.md).
