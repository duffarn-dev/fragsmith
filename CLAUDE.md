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

## Render PC
- **Main PC:** everything except rendering.
- **Render PC (with CS2 + CS Demo Manager):** runs `worker.py` (standard library
  only), which picks up render jobs from a job folder (SSH, shared folder or
  USB) and puts raw clips back. See `docs/two-pc-setup.md`.
- NEVER render in parallel — one CS2 window at a time.

## Fixed rules
- Read `learnings_runtime.md` as step 0 of every run — BEFORE anything else.
  That's main.py's job.
- English in all user-facing messages.
- Every csdm/ffmpeg command is logged verbatim with exit code + stderr.
- Failed clips are never swallowed silently — log and flag them in report.md.
- Temp files (raw clips) are deleted ONLY with `cleanup` or after confirmation.
- HLAE error in step 4 → stop the WHOLE run, don't retry blindly
  (game updates break HLAE; check whether csdm/HLAE needs an update).
- Personal data (SteamIDs, paths, hosts) lives only in `config.toml`, which
  is gitignored. Never commit it; `config.example.toml` holds placeholders.

## Known players
Configured in `config.toml` `[players].known_players`. The pipeline picks
whichever of them is in the demo; if several are, the highest total hype
score wins. `--player <steamid64>` overrides.

## Notes
- `docs/dev-notes.md` — traps and patterns discovered while BUILDING the
  project. Read before changing code here.
- `docs/troubleshooting.md` — known issues when RUNNING it.
- `learnings_runtime.md` — the user's own notes from runs. Suggest additions
  after a run; the user approves.
- `docs/effects.md` — every effect and its config flag.

## CLI
`run <demo>` (steps 0-3, phase stop) · `render <run-dir>` (step 4: queue the
job — THIS is the approval) · `finish <run-dir>` (steps 5-7, fetches clips
from the outbox itself) · `batch <folder>` · `cleanup <run-dir>`.

Tests: `python tests/test_*.py` (no pytest). See `tests/README.md`.
