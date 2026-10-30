# Fragsmith

Tar en CS2-demo (`.dem`) og lager automatisk en ferdig fragmovie (mp4) med de beste øyeblikkene, musikk-sync, slow-motion og overlays.

## Slik fungerer det

| Steg | Hva | Verktøy |
|---|---|---|
| 1 | Parser demoen: kills, headshots, våpen, clutcher | demoparser2 |
| 2 | Regelbasert hype-scoring av kill-clustere | script |
| 3 | AI Director velger klipp, rekkefølge og slow-mo | Claude (eneste LLM-steg) |
| 4 | Rendrer råklipp fra demoen | CS Demo Manager (`csdm video`) + HLAE |
| 5 | Beat-analyse av valgt musikkspor | librosa |
| 6–7 | Klipping, effekter og sammensetting til `final.mp4` | ffmpeg |

Det er en pause etter steg 3: du ser edit-planen og estimert render-tid før noe rendres.

## Oppsett

Krever Python, ffmpeg og en `ANTHROPIC_API_KEY` i `.env`. Kopier `config.example.toml` til `config.toml` og fyll inn dine verdier.

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

Rendering (steg 4) krever en Windows-PC med CS2 og CS Demo Manager installert. Det kan være samme PC eller en egen render-PC som kjører `worker.py`. Se [RENDER-PC-OPPSETT.md](RENDER-PC-OPPSETT.md).

Innstillinger (spillere, musikkmappe, effekter, render-oppløsning) ligger i `config.toml`.

## Bruk

```
python main.py run <demo.dem>        # steg 1–3, viser edit-plan
python main.py render runs/<id>      # steg 4, sender render-jobb til worker
python main.py finish runs/<id>      # steg 5–7, lager final.mp4
python main.py batch <mappe>         # steg 1–3 for alle demoer i en mappe
python main.py cleanup runs/<id>     # sletter råklipp og mellomfiler
```

## Begrensninger

- Kun `.dem`-input, ikke rå video.
- Kun lokal musikkmappe (ingen Suno/Udio-integrasjon).
- Kun spiller-POV, ikke cinematic kamera.
- CS2 spiller bare av ferske demoer (versjonslåst), og HLAE kan brekke midlertidig ved spilloppdateringer.

## Mer dokumentasjon

- [docs/oppgraderinger-spec.md](docs/oppgraderinger-spec.md): effektoppgraderinger
- `learnings_*.md` og [decisions.md](decisions.md): erfaringer og designvalg
