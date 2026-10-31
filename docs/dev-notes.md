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
- Commits that GitHub creates (the squash and merge buttons, `gh pr merge`) take the author name and email from the GitHub **account**, not from the local `git config`. Turn on "Keep my email addresses private" (github.com/settings/emails) before the first merge, or the account's primary email lands in public history. It happened here once: the squash commit had to be rewritten, and the old commit stays reachable through the PR until GitHub Support removes it.

## Testing

- `freezedetect`/YDIF can't verify a freeze frame when film grain is on, since grain changes every frame. Use a contact sheet (`fps=5` + `tile`) instead.
- See [tests/README.md](../tests/README.md) for test patterns.
