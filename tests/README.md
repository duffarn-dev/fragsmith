# Tests

Run them directly, no pytest needed:

    python tests/test_highfps.py
    python tests/test_beats.py
    python tests/test_drop_sync.py
    python tests/test_rules_director.py
    python tests/test_render_modes.py
    python tests/test_privacy_check.py

GitHub Actions runs every `tests/test_*.py` on each pull request (`.github/workflows/tests.yml`), so a new suite only needs to follow the same pattern: exit code 1 when anything fails.

## Patterns worth reusing

- **Mock clips**: use `testsrc2` at 720p/30fps, **not** 1080p60. The engine is supposed to normalize by itself, and mocks in the target format hide fps/resolution bugs (zoompan computes time as `on/60` and used to miss on 30 fps sources).
- **Effect verification**: compare PSNR with and without the effect, measured in time windows. The effect should give low PSNR inside its window and ~inf outside.
- **freezedetect does NOT work with grain** (temporal noise). Use a contact sheet (tiled frames) or PSNR between neighboring frames instead.
- Encode with `-preset ultrafast` in tests, and turn off `smooth_slowmo` (minterpolate) unless that's what you're testing.
