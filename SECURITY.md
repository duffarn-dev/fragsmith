# Security

## Reporting a problem

Please **don't open a public issue** for a security problem. Report it privately instead: on the repo's **Security** tab, choose **Report a vulnerability**. Only the maintainer sees the report.

Things worth reporting:

- personal data or a secret in the repo or its history (a Steam ID, an email address, a token, a key)
- a way for a demo file, a config file or a job file to make Fragsmith or the render worker run unintended commands
- anything in the two-PC setup (SSH or shared job folder) that exposes the render PC more than the docs say

This is a hobby project, so there's no fixed response time, but reports are read and taken seriously.

## What Fragsmith does to protect you

- **Personal settings stay local.** Steam IDs, paths and hosts live in `config.toml`, which is never committed (`config.example.toml` holds placeholders).
- **The render worker uses only the Python standard library.** It runs CS2 through CS Demo Manager in `-insecure` mode, the same way rendering in CS Demo Manager does. Don't queue for matchmaking while it renders.
- **Every pull request is checked automatically.** The check fails on commit email addresses that aren't GitHub noreply addresses, and on real Steam IDs, private IPs, user paths, tokens and keys (`tools/privacy_check.py`). GitHub secret scanning with push protection is on as well.
