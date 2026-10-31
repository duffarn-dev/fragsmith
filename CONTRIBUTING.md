# Contributing to Fragsmith

Thanks for wanting to help. Bug reports, effect ideas and pull requests are all welcome.

## Reporting a bug

Open an issue with:

- what you ran (`python main.py ...`) and what happened
- the output of `python main.py doctor`
- for render problems: `worker.log` (in the run folder with one PC, or the job folder with two)
- for editing problems: `runs/<id>/ffmpeg.log` and `report.md`

Please don't attach demos or `config.toml` publicly. They contain Steam IDs.

## Getting started

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Read [CLAUDE.md](CLAUDE.md) for the architecture in one page and [docs/dev-notes.md](docs/dev-notes.md) for the traps that have already bitten. Most of the interesting code is in `lib/edit_engine.py` (effects) and `lib/hype_score.py` (what counts as a highlight).

## Tests

The tests run directly, no pytest needed, and none of them need CS2:

```
python tests/test_beats.py
python tests/test_highfps.py
python tests/test_drop_sync.py
python tests/test_rules_director.py
python tests/test_render_modes.py
```

They need ffmpeg on your PATH. See [tests/README.md](tests/README.md) for patterns worth reusing, such as mock clips at 720p30 instead of 1080p60.

## Pull requests

- Branch from `develop` and open the PR against `develop`, not `main`. `main` only holds released code. The full branch model is in [docs/development.md](docs/development.md).
- Give the PR a [Conventional Commits](https://www.conventionalcommits.org/) title (`feat: ...`, `fix: ...`, `docs: ...`). It becomes the commit message and the line in the release notes.
- Keep each PR to one change, and run the tests before you open it.
- Match the style of the code around your change: comments explain *why*, not *what*.
- New effects get a config flag (off by default if they're loud), an entry in [docs/effects.md](docs/effects.md), and must survive `[effects].enabled = false`.
- The LLM is only used in step 3, and it's optional. Everything else stays a deterministic script, and rules mode must keep working without the `anthropic` package.
- `worker.py` uses the standard library only, so the render PC needs nothing but Python.
- Never commit personal data: no `config.toml`, `.env`, demos or real Steam IDs (use `76561190000000000`-style placeholders in tests).

If you find a new trap (a csdm quirk, an ffmpeg gotcha), add a line to [docs/dev-notes.md](docs/dev-notes.md).
