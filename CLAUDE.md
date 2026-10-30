# Fragsmith — regler for Claude Code

Autonom pipeline: CS2 .dem-fil → ferdig fragmovie (mp4) med musikk-sync,
slow-motion og overlays.

## Arkitektur (kort)
- **Steg 1** parse demo (demoparser2) → `runs/<id>/00-events.json`
- **Steg 2** regelbasert hype-scoring → `runs/<id>/01-candidates.json` (maks 25)
- **Steg 3** AI Director (Claude + Pydantic, retry maks 3) → `02-edit-plan.json`
- **FASESTOPP:** vis edit-plan + estimert render-tid/disk, vent på OK
- **Steg 4** render råklipp via `csdm video` (kjører på RENDER-PC-en, se under).
  P20: klipp som skal i slow-mo (Director-segmenter fra steg 3, headshot-
  mikro eller replay — delt logikk: `edit_engine.wants_high_fps` +
  `pick_replay_clip`) rendres i `[render].highfps_framerate` (120). csdm
  har GLOBALT framerate, så workeren kjører ett pass per framerate (egen
  CS2-oppstart per pass). Steg 6 bremser høy-fps-kilder med ekte frames
  (setpts) og hopper over minterpolate (P8) for dem.
- **Steg 5** beat-analyse (librosa) — kan gå parallelt med steg 4
- **Steg 6** editing engine (rå ffmpeg via subprocess — IKKE moviepy)
- **Steg 7** assembly → `final.mp4` + `report.md`

LLM brukes KUN i steg 3. Alt annet er deterministisk script.

## To-PC-oppsett
- **Denne PC-en (dev):** alt unntatt rendering.
- **Render-PC (med CS2 + CS Demo Manager):** kjører `worker.py` som plukker
  render-jobber fra en delt mappe og legger råklipp tilbake. I starten:
  USB-overføring i stedet for delt mappe — samme jobbformat.
- Rendering ALDRI parallelt — ett CS2-vindu om gangen.

## Faste regler
- Les `learnings_runtime.md` som steg 0 i enhver kjøring — FØR alt annet.
  Det er main.py sitt ansvar å gjøre dette.
- Norsk i alle brukervendte meldinger.
- Alle csdm/ffmpeg-kommandoer logges ordrett med exit code + stderr.
- Feilede klipp svelges aldri stille — logg og flagg i report.md.
- Temp-filer (råklipp) slettes KUN med `--cleanup` eller etter bekreftelse.
- HLAE-feil i steg 4 → stopp HELE kjøringen, ikke retry blindt
  (spilloppdateringer brekker HLAE; sjekk om csdm/HLAE trenger oppdatering).

## Kjente spillere
Konfigurert i config.toml `[players].known_players`. Pipelinen velger
automatisk den av disse som er i demoen; flere → høyest samlet hype-score.
`--player <steamid64>` overstyrer.

## Learnings
- `learnings_build.md` — feller/mønstre oppdaget under BYGGING av prosjektet.
  Les før du endrer kode her.
- `learnings_runtime.md` — mønstre fra KJØRINGER (demo-kilder som parser
  annerledes, HLAE-brekk, scoring-bommer, ffmpeg-artefakter).
  Agenten foreslår tillegg etter hver kjøring; brukeren godkjenner.
- Ikke bland disse med idea-factory sine learnings-filer i rot — de handler
  om fabrikkprosessen, ikke dette prosjektet.

## Status (oppdateres per fase)
- [x] Fase a: skjelett + parse + hype-scoring
- [x] Fase b: AI Director
- [x] Fase c: csdm-wrapper + worker for render-PC — VERIFISERT med ekte
      rendering 2026-07-04 (3 filmer levert: nuke, inferno, cache)
- [x] Fase d: beats (librosa) + editing engine — testet med mock-klipp
- [x] Fase e: assembly → final.mp4 + report.md — testet med mock-klipp
- [x] Fase f: batch-kø (steg 0-3) + processed.json + cleanup-kommando

CLI: `run <demo>` (steg 0-3, fasestopp) · `render <run-dir>` (steg 4:
legg jobb i kø — DETTE er godkjenningen) · `finish <run-dir>` (steg 5-7,
henter klipp fra outbox selv) · `batch <mappe>` · `cleanup <run-dir>`.
Worker på render-PC-en: worker.py (kun stdlib) — se RENDER-PC-OPPSETT.md.
