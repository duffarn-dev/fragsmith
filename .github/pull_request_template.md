## What and why

<!-- One change per PR. What does it do, and why is it needed? Link the issue if there is one: "Closes #12". -->

## How it was tested

<!-- Which test suites you ran, and for effects or rendering: what you checked in a real film. -->

## Checklist

- [ ] The title follows Conventional Commits (`feat:`, `fix:`, `docs:`, ...). It becomes the line in the release notes.
- [ ] The PR targets `develop` (or `main` for a hotfix).
- [ ] Tests pass locally (`python tests/test_*.py`), and new behavior has a test.
- [ ] Docs are updated (README, `docs/effects.md` for effects, `config.example.toml` for new settings).
- [ ] No personal data: no `config.toml`, `.env`, demos, real Steam IDs, user names or IPs.
- [ ] A non-obvious trap I ran into is written down in `docs/dev-notes.md` (or `docs/troubleshooting.md` if users can hit it).
