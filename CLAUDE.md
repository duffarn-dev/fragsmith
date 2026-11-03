# Fragsmith — rules for Claude Code

Autonomous pipeline: CS2 `.dem` file → finished fragmovie (mp4) with music sync, slow motion and overlays.

## Architecture (short)
- **Step 1** parse demo (demoparser2) → `runs/<id>/00-events.json`
- **Step 2** rule-based hype scoring → `runs/<id>/01-candidates.json` (max 25)
- **Step 3** Director → `02-edit-plan.json`. `[director].mode`: `auto` (Claude
  if `ANTHROPIC_API_KEY` is set, else rules; falls back to rules if Claude
  fails), `claude` (Claude + Pydantic, max 3 retries) or `rules`
  (`lib/rules_director.py`, no LLM). Both plans pass the same
  `director.semantic_errors` check.
- **PHASE STOP:** show the edit plan + estimated render time/disk, wait for OK
- **Step 4** render raw clips via `csdm video` (runs on the render PC, see below).
  Clips that will get slow-mo (Director segments from step 3, headshot micro
  slow-mo or replay — shared logic: `edit_engine.wants_high_fps` +
  `pick_replay_clip`) are rendered at `[render].highfps_framerate` (120). csdm
  has a GLOBAL framerate, so the worker runs one pass per framerate (separate
  CS2 launch per pass). Step 6 slows high-fps sources with real frames
  (setpts) and skips minterpolate for them.
- **Step 5** beat analysis (librosa) — can run in parallel with step 4
- **Step 6** editing engine (raw ffmpeg via subprocess — NOT moviepy)
- **Step 7** assembly → `final.mp4` + `report.md`

The LLM is used ONLY in step 3, and is optional. Everything else is a
deterministic script. `anthropic` is imported lazily, so rules mode must keep
working without the package installed.

## Rendering: one PC or two (`[render].mode`, `render_clips.render_mode`)
- **`local`:** one PC. `main.py render` calls `worker.render_job()` directly
  into `runs/<id>/03-raw-clips/` (no queue, no demo copy).
- **`ssh` / `folder`:** a render PC (with CS2 + CS Demo Manager) runs
  `worker.py` (standard library only), which picks up jobs from a job folder
  (SSH, shared folder or USB) and puts raw clips back. See `docs/two-pc-setup.md`.
- No `mode` in an old config → inferred: `ssh_host` set → ssh, else folder.
- Both paths share `worker.render_job()`; keep the queue logic in `process_job`.
- `main.py doctor` checks the setup, `main.py setup` writes `config.toml`
  (`lib/doctor.py`).
- NEVER render in parallel — one CS2 window at a time.

## Fixed rules
- Read `learnings_runtime.md` as step 0 of every run — BEFORE anything else.
  That's main.py's job.
- English in all user-facing messages. The one exception: Discord
  notifications to the maintainer (`tools/notify.py`) are in Norwegian.
- Every csdm/ffmpeg command is logged verbatim with exit code + stderr.
- Failed clips are never swallowed silently — log and flag them in report.md.
- Temp files (raw clips) are deleted ONLY with `cleanup` or after confirmation.
- HLAE error in step 4 → stop the WHOLE run, don't retry blindly
  (game updates break HLAE; check whether csdm/HLAE needs an update).
- Personal data (SteamIDs, paths, hosts) lives only in `config.toml`, which
  is gitignored. Never commit it; `config.example.toml` holds placeholders.
  The pre-commit hook (`tools/privacy_check.py --staged`) enforces this:
  never bypass it with `--no-verify`. Deliberate examples get a
  `privacy-check: ignore` marker on the line.

## Git workflow (`docs/development.md`)
- `main` = released, `develop` = next version. Both are protected: NEVER push
  to them directly; every change is a PR.
- Branch `feature/…`, `fix/…` or `docs/…` from `develop`, PR to `develop`,
  squash merge. PR title in Conventional Commits form (`feat:`, `fix:`, …).
- Release = PR `develop` → `main`, merge commit (never squash). Bump
  `lib/version.py` first in a `chore(release): vX.Y.Z` PR to `develop`; the
  `release` workflow tags `main` and publishes the notes (`docs/releasing.md`).
- Hotfix = branch from `main`, PR to `main` (merge commit), then PR `main` →
  `develop` (merge commit, NOT squash).

## Known players
Configured in `config.toml` `[players].known_players`. The pipeline picks
whichever of them is in the demo; if several are, the highest total hype
score wins. `--player <steamid64>` overrides.

## Notes
- `docs/dev-notes.md` — traps and patterns discovered while BUILDING the
  project. Read before changing code here. When something goes wrong in a
  non-obvious way, add a line here (or to `troubleshooting.md` if users can
  hit it) in the SAME PR as the fix, and mention it in the status.
- `docs/troubleshooting.md` — known issues when RUNNING it.
- `learnings_runtime.md` — the user's own notes from runs. Suggest additions
  after a run; the user approves.
- `docs/effects.md` — every effect and its config flag.
- `docs/compatibility.md` — tested versions (`compat/tested-versions.json`,
  only changed after a real end-to-end test), the daily upstream watcher
  (`tools/upstream_watch.py`) and what to do when an update breaks things.

## CLI
`run <demo>` (steps 0-3, phase stop; `--style` picks a preset from
`lib/styles.py`, stored in the plan so render/finish use the same one) · `render <run-dir>` (step 4: queue the
job — THIS is the approval) · `finish <run-dir>` (steps 5-7, fetches clips
from the outbox itself; `--vertical` adds a 9:16 pass with `Frame` PORTRAIT) · `batch <folder>` · `cleanup <run-dir>` · `selftest` (`lib/selftest.py`: two
clips end to end through the real `cmd_render`/`cmd_finish`, version check,
`--write-tested`).

Tests: `python tests/test_*.py` (no pytest). See `tests/README.md`.
