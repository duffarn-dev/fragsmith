# Oppsett av render-PC-en (engangsjobb)

Render-PC-en gjør bare én ting: kjører `worker.py`, som plukker render-jobber
fra en jobbmappe, starter CS2 via CS Demo Manager/HLAE og legger råklipp
tilbake. Alt annet (parsing, AI Director, redigering, musikk) skjer på dev-PC-en.

## Krav på render-PC-en

1. **CS2** installert via Steam
2. **CS Demo Manager** (installeres som standard i
   `%LOCALAPPDATA%\Programs\cs-demo-manager` — `csdm.exe`
   ligger der; HLAE og FFmpeg installeres automatisk av csdm ved første kjøring)
3. **Python 3.10+** — installer fra python.org hvis den mangler
   (worker.py bruker kun standardbiblioteket, ingen pip-pakker)
4. **worker.py** — kopier fila fra dette prosjektet til render-PC-en

## Viktig å vite om CS Demo Manager

- Start CS Demo Manager-appen ÉN gang først og fullfør oppstartsveiviseren
  (den setter opp PostgreSQL-databasen som CLI-et bruker).
- Rendering starter CS2 med `-insecure` — helt normalt, men ikke spill
  VAC-beskyttede matcher mens workeren jobber.
- PC-en er OPPTATT under rendering: CS2 kjører i forgrunnen. Ikke rør den.
- Spilloppdateringer brekker HLAE med jevne mellomrom. Symptom: `csdm video`
  feiler rett etter en CS2-oppdatering. Fiks: oppdater CS Demo Manager
  (den henter ny HLAE), prøv igjen. Workeren stopper selv etter 2 feil på rad.

## Velg jobbmappe

**Alternativ A — delt mappe (anbefalt):**
1. Del en mappe på render-PC-en (f.eks. `C:\fragsmith-jobs`) i Windows:
   høyreklikk → Egenskaper → Deling → Del med din bruker.
2. På dev-PC-en: sett stien i `config.toml`:
   ```toml
   [render]
   job_dir = '\\RENDER-PC-NAVN\fragsmith-jobs'
   ```

**Alternativ B — USB:** la `job_dir` stå tom i config.toml; `render`-kommandoen
skriver da jobbfila lokalt og forteller deg nøyaktig hva som skal kopieres.

## Start workeren

```
python worker.py --job-dir C:\fragsmith-jobs
```

(Legg til `--csdm "C:\...\csdm.exe"` hvis csdm ligger et uvanlig sted,
`--once` for å prosessere én jobb og avslutte.)

Workeren logger til konsollen og `<job_dir>\worker.log`.

## Flyten ende-til-ende

```
dev-PC:    python main.py run <demo.dem>      → edit-plan + fasestopp
dev-PC:    python main.py render runs/<id>    → jobb + demo inn i køen
render-PC: worker.py plukker jobben           → CS2 rendrer klippene
dev-PC:    python main.py finish runs/<id>    → henter klipp, musikk, final.mp4
dev-PC:    python main.py cleanup runs/<id>   → slett råklipp (etter at du har
                                                 sett filmen)
```
