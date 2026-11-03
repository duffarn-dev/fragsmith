# Developer notes

Traps and facts discovered while building Fragsmith. Read this before changing the editing engine or the render worker. Format: `- <what> — <consequence/rule>`

## Demo parsing

- demoparser2 fails at random: about 1 parse in 20 raises (IllegalPathOp, VectorResizeFailure, EntityNotFound) or segfaults the whole process, with both 0.41.3 and 0.42.1, on demos that parse fine the next time. `round_end` can also come back empty without raising. `parse_demo` therefore parses in a child process with up to 3 attempts, and re-reads rounds with a fresh parser if they come back empty. Don't "simplify" this back to an in-process call.
- Valve matchmaking demos can lack `round_end` for the last round (surrender), so `total_rounds_played` points outside the round list. `parse_demo` synthesizes a final round without a winner.
- Valve matchmaking stores demos as `.dem.bz2`. `main.py` extracts them automatically and keeps the `.bz2`.
- demoparser2 columns match the documentation on Valve MM demos (`attacker_`/`user_` prefixes, `total_rounds_played`, `is_warmup_period`, `team_num` 2=T / 3=CT).
- Silent clutches (won without kills in the clutch phase, e.g. on time or defuse) used to flag earlier kills in the round as clutch kills. `detect_won_clutches` now returns `from_tick`, and only clips ending after it get the flag.
- `player_death` has kill context columns: `noscope`, `thrusmoke`, `penetrated` (the number of surfaces, so >0 means a wallbang), `attackerinair`, `attackerblind` and `distance` (meters in CS2). Older demos or parsers can lack them, and pandas gives NaN for gaps: `bool(NaN)` is **True**, so read them through `kill_context()` in `parse_demo.py`, never with a bare `bool()`.
- `player_death` also fires for teamkills and suicides, with nothing that marks them. Hype scoring used to count every death with an attacker, so an AWP headshot on a teammate turned a 2K into a "3K" with a headshot punch on the teamkill (#41). `hype_score.counts_as_kill` is now the one rule: an attacker, not the victim, and not the same known team (`attacker_team`/`victim_team` come from `team_num`). Scoring, the stats outro and `report.md` use it. Anything new that counts kills (tags, counters) should read the clip's `Highlight.kills` marks, which only hold real kills, not `events["kills"]`. `detect_won_clutches` deliberately reads every death, since a teamkilled teammate is gone from the round either way.
- Without `known_players`, the POV is the player with the highest total hype score, so a scoring change can flip who gets filmed. On the showcase demo, dropping one teamkill was enough to make another player the fallback POV.

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
- A job that failed used to stay in `working/`, and the worker re-queues `working/` on start (meant for crashes). Restarting the worker therefore re-rendered a job that had failed (seen in the M7 selftest: it failed because the CS Demo Manager app was open, then rendered after the restart). Failed jobs now go to `failed/`.
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
- The output size comes from a `Frame` (`lib/edit_engine.py`: `LANDSCAPE` 1920x1080 fit+pad, `PORTRAIT` 1080x1920 center crop) passed through the engine, cards and cold open. Don't write a size into a filter string; use `frame.size`, `frame.normalize()` or `frame.w`/`frame.h`.
- The 9:16 film reuses the landscape timing (cut log, drop sync): nothing in `_timing_plan` depends on the frame, and `tests/test_frame.py` checks that the segment durations are equal. Keep it that way, or the vertical film drifts off the beat.
- Portrait layout: `Frame.portrait` moves things out of the bands a Shorts/TikTok player covers (the bottom ~20 %: captions and buttons; the top ~8 %: the app bar). That means the lower third at 78 %, REPLAY at 12 % and the logo at the top. The logo width is a share of the frame's long side, so it's the same size in px in both films.
- How the frame refactor was proven to change nothing: run `finish` on the same run before and after, then diff the `cmd:` lines in `ffmpeg.log`. They were byte-identical. Use the same check for any refactor of filter-building code.
- Overlaying an RGBA png (the logo) lets ffmpeg pick `gbrp` for the output, and libx264 then encodes RGB that players show purple and green. Force `format=yuv420p` after the overlay and keep `-pix_fmt yuv420p`.
- A single-image overlay input (`-i logo.png`) stopped showing after the first part of a concat input. Loop it (`-loop 1 -i logo.png`) and end it with `overlay=...:shortest=1`.
- Always loop it, even for one output frame: with an input seek that lands between two frames (19.056 s in a 120 fps clip), the first video frame has a pts above 0, the unlooped logo ends first, `shortest=1` ends the graph, and ffmpeg **exits 0 without writing the file**. Tests that seek onto an exact frame don't catch it. Check that the output exists.
- `drawbox=c=black@0.35:t=fill` darkens in YUV, so it also pulls colors toward gray (yellow got a blue cast in the test). Pure red or blue test images have so little luma that "darkened" looks black; test with bright or gray images.
- Fine film grain doesn't survive the encode: libx264 at crf 18 smooths `noise=alls=3` (`grain` 0.25 at intensity 1.0) away, and a 1:1 crop looked the same as no grain. Check grain at 1:1 or by file size: noise costs bits, and 0.5 made the film ~7 % bigger.

## drawtext

- ASCII apostrophes can't be escaped robustly through both the filter-graph and drawtext parsing levels. `drawtext_escape` replaces `'` with a typographic `’`.
- With `expansion=normal` (the default) there's no working escape for a literal `%`. **All** drawtext calls use `:expansion=none` (we never use `%{...}`), and `drawtext_escape` doesn't touch `%`.

## Audio

- Synthesized sound must be peak-checked with `astats`. `volume=2.0` after `amix` clipped `impact.wav` into a square wave in 16-bit. Rule: always `alimiter=limit=0.95` as the last step of SFX synthesis, and check that the peak level is below -1 dB.
- ffmpeg's `sine` source is quiet (~-18 dB), so "audible" needs a strong boost.
- **A stream-copied concat of AAC parts drifts the audio.** Every part keeps its AAC priming (1024 samples) and its padding, but the video timestamps only follow each part's duration. The decoded audio ran ~27 ms ahead per join, so the game sound fell behind the picture: 211 ms after 8 parts in the test, and 140–230 ms in a real film before 2026-11-02. The mix doesn't use the timeline's audio. It decodes each part on its own (which drops its priming), cuts or pads it to the part's length, and joins them with the `concat` filter. `tests/test_audio_sync.py` checks it with a flash and a beep in every part.
- **The concat step starts the picture ~21 ms in.** That's the first part's AAC priming. The mix starts the audio at the video's `start_time` (`asetpts=PTS+x/TB`), so it isn't ahead of the picture.
- **Measure audio and video sync with a flash and a beep, not by ear.** Find the flash with `signalstats` (YAVG), and the beep by scanning the samples. Step through them 7 at a time, not 48: 48 samples is one period of a 1 kHz tone, so every probe can land on a zero crossing. For a real film, cross-correlate each part's own audio against the film, high-passed at 2 kHz so the music's bass doesn't win.
- **`aeval` time: use `n/s`, not `t`, to line up with `adelay`.** `t` follows the frame timestamps, while `adelay` counts samples. On a timeline with timestamp gaps or overlaps, the drop gap landed 50 ms late with `t`.
- **`alimiter` has two defaults that bite in a mix.** `level=true` auto-gains the output, and the lookahead delays it unless `latency=true`. In a final mix, use `alimiter=limit=0.95:level=false:latency=true`, or the drop lands late and the level changes.
- **`afade=curve=exp` over a long riser is inaudible until the very end.** It measured -107 dB at the start and -29 dB at 3 s of 4. `curve=qua` swells audibly the whole way.
- **A fixed riser level overshoots a quiet build-up.** The bars before a drop are often the quietest part of a track. In a Suno track they were at -19 dB RMS, against -11 dB after the drop. A riser tuned on typical music ended 10 dB over the music, and it was heard as a loud sound on top of the track. The riser's gain now comes from the music under it, measured with `volumedetect`. To check a real film, compare its RMS with the music track's in 0.25 s windows, at the music offset from `report.md`.
- **Measure one channel in tests, not a mono downmix.** `-ac 1` sums the channels at -3 dB each, so identical channels come out 3 dB louder, with peaks over 1.0. Use `pan=mono|c0=c0`.

## Timeline

- **Drop sync can't reach every drop beat.** The cut into the climax snaps to a beat from where the clip before it snapped, so moving the music offset can skip a beat: on a 99 BPM track the climax went from 604 ms early straight to 581 ms late. The old check took anything inside the beat tolerance (0.75 s), so the film had the gap and the boom on the drop and the cut a beat later. A hit is now within 100 ms; otherwise drop sync tries the next drop.
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
