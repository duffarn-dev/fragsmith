# Developer notes

Traps and facts discovered while building Fragsmith. Read this before changing the editing engine or the render worker. Format: `- <what> — <consequence/rule>`

## Demo parsing

- demoparser2 fails at random: about 1 parse in 20 raises (IllegalPathOp, VectorResizeFailure, EntityNotFound) or segfaults the whole process, with both 0.41.3 and 0.42.1, on demos that parse fine the next time. `round_end` can also come back empty without raising. `parse_demo` therefore parses in a child process with up to 3 attempts, and re-reads rounds with a fresh parser if they come back empty. Don't "simplify" this back to an in-process call.
- Valve matchmaking demos can lack `round_end` for the last round (surrender), so `total_rounds_played` points outside the round list. `parse_demo` synthesizes a final round without a winner.
- Valve matchmaking stores demos as `.dem.bz2`. `main.py` extracts them automatically and keeps the `.bz2`.
- demoparser2 columns match the documentation on Valve MM demos (`attacker_`/`user_` prefixes, `total_rounds_played`, `is_warmup_period`, `team_num` 2=T / 3=CT).
- Silent clutches (won without kills in the clutch phase, e.g. on time or defuse) used to flag earlier kills in the round as clutch kills. `detect_won_clutches` now returns `from_tick`, and only clips ending after it get the flag.

## CS Demo Manager CLI (csdm)

- cs-demo-manager.com blocks automated fetching. Read CLI facts from the source on GitHub (`akiver/cs-demo-manager`).
- A demo **must** be analyzed first (`csdm analyze`, which skips already analyzed demos).
- `--config-file` renders all sequences in **one** CS2 launch, which is much faster than one launch per clip.
- POV is set per sequence via `playerCameras: [{tick, playerSteamId, playerName}]`.
- Enums: `recordingSystem` HLAE|CS, `encoderSoftware` FFmpeg|VirtualDub, `recordingOutput` video|images|images-and-video.
- Output names: `sequence-<N>-tick-<start>-to-<end>.<container>`.
- The **default** ffmpeg container is AVI (+ mp3 audio, crf 23). Always send explicit `ffmpegSettings` (mp4/aac/crf 18). An early bug: the worker globbed `*.mp4` and deleted the raw folder containing the AVIs. Rule: never delete work files when the expected output is missing.
- The CS Demo Manager **desktop app** listens on port 4574, the same port `csdm video` uses for CS2's HLAE plugin. With the app open, CS2 connects to the app and `csdm video` waits forever with no output (and exits 0 when killed). `worker.render_job` refuses to start while the port is taken.
- Updating CS Demo Manager does not update HLAE; HLAE lives in `%USERPROFILE%\.csdm\hlae` and is updated from the app's Settings → Video. csdm rewrites `hlae\ffmpeg\ffmpeg.ini` (pointing at its own ffmpeg) on every render.
- `framerate` is **global** in the video config, not per sequence (`src/common/types/sequence.ts`). High-fps rendering therefore runs one render pass per framerate (separate CS2 launch, ~1–2 min extra).
- Sequences have a `cfg` field (per-sequence console commands). That's the way in for in-game FOV zoom.

## Render worker

- Microsoft Store Python shows up as `python3.12.exe` in the process list, so `taskkill /IM python.exe` misses it.
- A running worker doesn't load new code. Restart it after changing `worker.py` and check `worker.log` for a "worker started" line with the right timestamp.

## ffmpeg filter graphs

- The `concat` filter requires the **same resolution** on all inputs. Normalize resolution per part *before* concat.
- The zoompan expressions compute time as `on/60` and assume 60 fps input. Normalize with `fps=60` per part before concat.
- Rule from the two above: test `edit_engine` with mock clips in a **different resolution and fps** than 1080p60. csdm clips are always 1080p60, which hides these bugs.
- An empty filter chain (`[vc][vout]`) is invalid in `filter_complex`. With all effects off it must fall back to the `null` filter.
- On very long clips (80 s, 27 concat parts) content drifts ~0.5 s from accumulated frame rounding. The effects drift with it, so the film looks right, but the tail is trimmed. Negligible on normal clips (8–20 s); verify effect placement visually on clips longer than ~40 s.
- `rgbashift` forces YUV→RGB→YUV on the **whole** stream (format negotiation ignores `enable`) and loses color everywhere. Use `chromashift` (native yuv420p) for channel-shift effects.
- In `drawbox` expressions, `t` means **thickness**, not time. Time-animated position needs `overlay` with `t` in the x/y expressions.
- Verify animations with `signalstats`/YAVG per frame, not by looking at a single frame.

## drawtext

- ASCII apostrophes can't be escaped robustly through both the filter-graph and drawtext parsing levels. `drawtext_escape` replaces `'` with a typographic `’`.
- With `expansion=normal` (the default) there's no working escape for a literal `%`. **All** drawtext calls use `:expansion=none` (we never use `%{...}`), and `drawtext_escape` doesn't touch `%`.

## Audio

- Synthesized sound must be peak-checked with `astats`. `volume=2.0` after `amix` clipped `impact.wav` into a square wave in 16-bit. Rule: always `alimiter=limit=0.95` as the last step of SFX synthesis, and check that the peak level is below -1 dB.
- ffmpeg's `sine` source is quiet (~-18 dB), so "audible" needs a strong boost.

## Timeline

- Effects that add content at the **front** of the timeline (the cold open) must be counted into the beat-sync start position. Forgetting this put all beat sync ~0.75 s off with `intro_hook` on.

## GitHub workflow

- Branch rulesets can limit the merge method per branch (`allowed_merge_methods` in the `pull_request` rule). `main` allows only merge commits, `develop` allows squash and merge. `develop` needs both: squash for feature PRs, merge for syncing a hotfix back from `main`. A squashed sync would give `develop` a copy of the hotfix commit instead of the commit itself, and the next release would carry both.
- The rulesets have no bypass actors, so the repo owner can't push to `main` or `develop` either. That's intended: every change is a PR.
- CI runs after a push, when the branch is already public on GitHub (PR refs keep commits reachable even after the branch is deleted). A CI privacy check can only stop a merge, not a leak. The pre-commit hook (`.githooks/pre-commit` → `tools/privacy_check.py --staged`) is the real guard. CI is the safety net.
- Scheduled workflows always run on the default branch (`main`). The Monday test run therefore tests the released code against new dependency releases, which is what users hit.
- Dependabot **security** updates always open PRs against the default branch, ignoring `target-branch`. That would bypass `develop`, so only Dependabot *alerts* are on, and version updates are limited to GitHub Actions.
- With `core.autocrlf=true` (the Git for Windows default), shell hooks get CRLF line endings on checkout and break under `sh`. `.gitattributes` pins `.githooks/*` to LF.
- `gh pr merge` suggests `--admin` when a PR is blocked. It doesn't help here: the rulesets have no bypass actors, so `current_user_can_bypass` is `never`, even for the owner.
- The `pr-title` workflow uses `pull_request`, not `pull_request_target`. `pull_request_target` would give fork PRs a write token, but its check runs attach to the base commit, which is a poor fit for a required check. The cost: fork and Dependabot PRs get a read-only token, so the workflow only checks their title and doesn't label them.
- A PR title is untrusted input. Workflows pass it through `env:` and never interpolate `${{ github.event.pull_request.title }}` into a `run:` script, where a crafted title could inject shell commands.
- Release notes come from PR labels. A PR without a type label lands under "Other", so `tools/pr_title.py` derives the label from the title instead of relying on people.
- Issue forms, `config.yml` and the PR template are read from the **default branch** only. The GraphQL `issueTemplates` field doesn't list issue *forms*, so check them in a browser at `/issues/new/choose`.
- A required issue-form dropdown preselects its first option, so a hasty reporter submits a wrong answer without noticing. Optional dropdowns show "None" instead.
- With private vulnerability reporting on, GitHub adds its own "Report a security vulnerability" entry to the issue chooser. Don't add a contact link for it as well.
- A PR whose merge commit was rewritten afterwards (force push) disappears from generated release notes: GitHub can't link it to the history anymore. v0.1.0 lists PR #1 by hand for that reason. Changes to them on `develop` show up on GitHub after the next release to `main`.
- Commits that GitHub creates (the squash and merge buttons, `gh pr merge`) take the author name and email from the GitHub **account**, not from the local `git config`. Turn on "Keep my email addresses private" (github.com/settings/emails) before the first merge, or the account's primary email lands in public history. It happened here once: the squash commit had to be rewritten, and the old commit stays reachable through the PR until GitHub Support removes it.

## Selftest and versions

- A CS2 demo's header (`demoparser2` `parse_header()`) says which game version recorded it: `patch_version` is steam.inf's `PatchVersion` without dots (1.41.9.4 → `14194`), and `game_directory` contains `csgo_v<ClientVersion>`. So "is this demo fresh?" can be checked exactly, before rendering, instead of guessing from the file date (which is the download date, not the match date).
- HLAE's and CS Demo Manager's versions are the Windows file versions of `%USERPROFILE%\.csdm\hlae\HLAE.exe` and `cs-demo-manager.exe` (next to `csdm.cmd`). `worker.file_version` reads them with `ctypes` (version.dll), so the worker stays standard-library only. They're four-part (`2.192.10.0`); the selftest compares the first three, as the GitHub tags have them.
- The worker puts those versions in every render result, so the main PC learns what the render PC runs without an extra SSH call. A worker copied before that change returns results without `versions`, which the selftest reports as "the worker is older than this Fragsmith".
- `selftest` takes `cmd_render` and `cmd_finish` as arguments from `main.py` instead of importing them (that would be circular). It tests the real commands, not a copy.

## Upstream watcher

- Steam's `ISteamApps/UpToDateCheck/v1/?appid=730&version=0` answers with the current CS2 version in `message` ("Server version required: 1.41.9.4"), the same format as `PatchVersion` in the installed `game/csgo/steam.inf`. No API key needed. The same goes for GitHub's `releases/latest` (which skips drafts and pre-releases) and PyPI's JSON API.
- GitHub's issue list with a label filter lags a few seconds behind a newly created issue. Two watcher runs right after each other can therefore open a duplicate (it happened in the M5 test: #10 and #11). Harmless with a daily schedule; just don't run it twice in a row.
- Closing keywords (`Closes #12`) only work for PRs into the default branch. The tested-versions PR goes to `develop`, so the watcher closes an `upstream` issue itself once its version is in `compat/tested-versions.json` on `main`.

## Notifications

- Discord webhooks ping whoever a message mentions. The texts include commit titles, which anyone with a PR can write, so `tools/notify.py` always sends `allowed_mentions: {"parse": []}`. Without it, a commit titled `@everyone` would ping the whole server.
- Repo secrets aren't given to workflows triggered by fork PRs (or Dependabot). Notifications only run on push, schedule and manual events, and `notify.py` exits 0 when the secret is missing.
- `workflow_dispatch` workflows (like `notify-test`) can only be started once the file is on the default branch.

## Testing

- `freezedetect`/YDIF can't verify a freeze frame when film grain is on, since grain changes every frame. Use a contact sheet (`fps=5` + `tile`) instead.
- See [tests/README.md](../tests/README.md) for test patterns.
