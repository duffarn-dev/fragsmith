# Fragsmith

Turn a CS2 demo (`.dem`) into a finished fragmovie (mp4) automatically, with the best moments, music sync, slow motion and overlays.

## How it works

| Step | What | Tool |
|---|---|---|
| 1 | Parses the demo: kills, headshots, weapons, clutches | demoparser2 |
| 2 | Rule-based hype scoring of kill clusters | script |
| 3 | AI Director picks clips, order and slow-mo | Claude (the only LLM step) |
| 4 | Renders raw clips from the demo | CS Demo Manager (`csdm video`) + HLAE |
| 5 | Beat analysis of the chosen music track | librosa |
| 6–7 | Cutting, effects and assembly into `final.mp4` | ffmpeg |

There's a pause after step 3: you see the edit plan and the estimated render time before anything is rendered.

## Setup

Requires Python, ffmpeg and an `ANTHROPIC_API_KEY` in `.env`.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.toml config.toml
```

Then fill in your SteamID64 and music folder in `config.toml`.

Rendering (step 4) needs a Windows PC with CS2 and CS Demo Manager installed. It can be the same PC or a separate render PC running `worker.py`. See [docs/two-pc-setup.md](docs/two-pc-setup.md).

## Usage

```
python main.py run <demo.dem>        # steps 1–3, shows the edit plan
python main.py render runs/<id>      # step 4, sends the render job to the worker
python main.py finish runs/<id>      # steps 5–7, creates final.mp4
python main.py batch <folder>        # steps 1–3 for every demo in a folder
python main.py cleanup runs/<id>     # deletes raw clips and intermediate files
```

## Limitations

- `.dem` input only, not raw video.
- Music from a local folder only.
- Player POV only, no cinematic camera paths.
- CS2 only plays back fresh demos (version-locked), and HLAE can break temporarily after game updates.

## Documentation

- [docs/effects.md](docs/effects.md): every effect and its config flag
- [docs/two-pc-setup.md](docs/two-pc-setup.md): rendering on a separate PC
- [docs/troubleshooting.md](docs/troubleshooting.md): known issues
- [docs/dev-notes.md](docs/dev-notes.md): notes for contributors
