# Contributing to Fragsmith

Thanks for wanting to help. Bug reports, effect ideas and pull requests are all welcome.

## Reporting a bug

Open an issue and pick **Bug report**. The form asks for:

- what you ran (`python main.py ...`) and what happened
- the output of `python main.py doctor`
- for render problems: `worker.log` (in the run folder with one PC, or the job folder with two)
- for editing problems: `runs/<id>/ffmpeg.log` and `report.md`

Please don't attach demos or `config.toml` publicly. They contain Steam IDs. The `doctor` output shows your music folder, SSH host and key path, so replace your user name and IP before pasting. Security or privacy problems go to the **Security** tab, not an issue (see [SECURITY.md](SECURITY.md)).

## Getting started

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
git config core.hooksPath .githooks
```

The last line turns on a pre-commit hook that stops commits containing personal data. Commits must use your GitHub noreply email address (github.com/settings/emails). Pull requests with any other commit email fail the `privacy` check. See [docs/development.md](docs/development.md#one-time-setup-per-clone).

Read [CLAUDE.md](CLAUDE.md) for the architecture in one page and [docs/dev-notes.md](docs/dev-notes.md) for the traps that have already bitten. Most of the interesting code is in `lib/edit_engine.py` (effects) and `lib/hype_score.py` (what counts as a highlight).

## Tests

The tests run directly, no pytest needed, and none of them need CS2:

```
python tests/test_beats.py
python tests/test_highfps.py
python tests/test_drop_sync.py
python tests/test_rules_director.py
python tests/test_render_modes.py
python tests/test_privacy_check.py
python tests/test_release.py
python tests/test_upstream_watch.py
python tests/test_notify.py
python tests/test_selftest.py
python tests/test_styles.py
python tests/test_branding.py
python tests/test_thumbnail.py
python tests/test_frame.py
python tests/test_kill_tags.py
```

They need ffmpeg on your PATH. GitHub runs them on every pull request as well. See [tests/README.md](tests/README.md) for patterns worth reusing, such as mock clips at 720p30 instead of 1080p60.

## Pull requests

- Bigger features: check [ROADMAP.md](ROADMAP.md) and comment on the issue first, so two people don't build the same thing.
- Branch from `develop` and open the PR against `develop`, not `main`. `main` only holds released code. The full branch model is in [docs/development.md](docs/development.md).
- Give the PR a [Conventional Commits](https://www.conventionalcommits.org/) title (`feat: ...`, `fix: ...`, `docs: ...`). It becomes the commit message and the line in the release notes, and the `pr-title` check sets the labels from it.
- Keep each PR to one change, and run the tests before you open it.
- Match the style of the code around your change: comments explain *why*, not *what*.
- New effects get a config flag (off by default if they're loud), an entry in [docs/effects.md](docs/effects.md), and must survive `[effects].enabled = false`.
- The LLM is only used in step 3, and it's optional. Everything else stays a deterministic script, and rules mode must keep working without the `anthropic` package.
- `worker.py` uses the standard library only, so the render PC needs nothing but Python.
- Never commit personal data: no `config.toml`, `.env`, demos or real Steam IDs (use `76561190000000000`-style placeholders in tests).

If you find a new trap (a csdm quirk, an ffmpeg gotcha), add a line to [docs/dev-notes.md](docs/dev-notes.md).
