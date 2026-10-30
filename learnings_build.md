# Learnings — bygging (fragsmith)

Mønstre, feller og avgjørelser oppdaget under bygging av dette prosjektet.
Les FØR du endrer kode her. Format: `- YYYY-MM-DD: <hva> — <konsekvens/regel>`

- 2026-07-03: Prosjektet bygges på dev-PC uten CS2-avhengighet for steg 1-3
  og 5-7; steg 4 (csdm/HLAE) kan kun testes reelt på render-PC-en.
- 2026-07-03: Valve MM-demoer kan mangle round_end for siste runde (surrender)
  — total_rounds_played ga da rundenummer utenfor runde-lista. parse_demo
  syntetiserer nå en sluttrunde uten vinner.
- 2026-07-03: Windows-konsoll (cp1252) knekker på unicode-spillernavn —
  main.py reconfigurerer stdout til utf-8 med errors="replace".
- 2026-07-03: Valve MM lagrer demoer som .dem.bz2 — main.py pakker ut
  automatisk ved behov (beholder .bz2).
- 2026-07-03: demoparser2-kolonnene stemte med dokumentasjonen på Valve
  MM-demoer fra 2024 (attacker_/user_-prefiks, total_rounds_played,
  is_warmup_period, team_num 2=T/3=CT) — verifisert på 4 ekte demoer.
- 2026-07-03: csdm CLI (verifisert mot kildekoden, src/cli/commands/
  video-command.ts): demoen MÅ analyseres først (`csdm analyze`, hopper over
  allerede analyserte); `--config-file` tar alle sekvenser i ÉN CS2-oppstart
  (mye raskere enn per-klipp); POV settes per sekvens via playerCameras
  [{tick, playerSteamId, playerName}]; enums: recordingSystem HLAE|CS,
  encoderSoftware FFmpeg|VirtualDub, recordingOutput video|images|
  images-and-video. Output-navn (verifisert i kilde + ekte kjøring):
  sequence-<N>-tick-<start>-to-<slutt>.<container>.
- 2026-07-04: csdm sin DEFAULT ffmpeg-container er AVI (+ mp3-lyd, crf 23) —
  send alltid eksplisitt ffmpegSettings (mp4/aac/crf 18) i config-fila.
  Første ekte rendering "forsvant" fordi worker glob'et *.mp4 og slettet
  _raw med AVI-ene i. Regel: aldri slett arbeidsfiler når output mangler.
- 2026-07-04: Store-Python på render-PC-en heter python3.12.exe i prosess-
  lista — taskkill /IM python.exe bommer. Og: en kjørende worker laster ikke
  ny kode; prosessen MÅ restartes etter worker.py-oppdatering (sjekk
  worker.log for "worker startet"-linje med riktig tidspunkt).
- 2026-07-04: FØRSTE EKTE FILM LEVERT (de_nuke, 51 s): run → render
  (SSH-kø) → worker/CS2/HLAE → finish. Ekte rendering av 2 klipp (46 s
  gameplay) tok ~2 min på render-PC-en.
- 2026-07-03: cs-demo-manager.com blokkerer WebFetch (403) — les CLI-fakta
  fra GitHub-kilden (raw.githubusercontent.com/akiver/cs-demo-manager).
- 2026-07-05: ffmpeg `concat`-filteret krever samme oppløsning på ALLE
  inndeler — slowmo_punch-grenen skalerte sin del til 1080p før concat og
  krasjet på ikke-1080p-kilder (usynlig med csdm-klipp som alltid er 1080p;
  funnet med 720p-mock under P3/P8). Regel: normaliser oppløsning per del
  FØR concat, og test edit_engine med mock i annen oppløsning enn 1080p.
- 2026-07-05: tom filterkjede (`[vc][vout]`) er ugyldig i filter_complex —
  med alle effekter av + ingen overlay/fade må kjeden falle tilbake til
  `null`-filteret.
- 2026-07-05: zoompan-uttrykkene i edit_engine regner tid som `on/60` og
  antar 60 fps inn — en 30fps-mock ga alle tidsstyrte effekter (punch,
  freeze-plassering) på ~halv tid, usynlig med csdm-klipp som alltid er
  60 fps. Fikset med `fps=60` per del før concat (samme grep som
  oppløsnings-normaliseringen). Regel: test edit_engine med mock i både
  annen oppløsning OG annen fps enn 1080p60.
- 2026-07-05: på et 81 s-klipp med 27 concat-deler (P3-ramper + P10-micro +
  P4-split) gled innholdet ~0.5 s bakover mot slutten pga. akkumulert
  frame-avrunding per del — effektene (on/60-konvolutter) gled MED innholdet,
  så filmen ser riktig ut, men halen trimmes tilsvarende. Ubetydelig på
  normale klipp (8-20 s, ≤8 deler); verifiser effekt-plassering visuelt,
  ikke bare mot kalkulert output-tid, på klipp > ~40 s.
- 2026-07-05: freezedetect/YDIF kan IKKE verifisere P4-freeze når film grain
  (P5, noise allf=t) er på — grain endrer hver frame. Bruk kontaktark
  (fps=5 + tile) og se etter fryst innhold i stedet.
- 2026-07-05: syntetisert lyd MÅ peak-verifiseres med astats før bruk —
  `volume=2.0` etter amix klippet impact.wav til firkantbølge i 16-bit
  (høyfrekvent skurr på hvert drap i levert film). Og: ffmpeg `sine`-kilden
  er lavmælt (~-18 dB), så "hørbar" krever kraftig løft. Regel: alltid
  `alimiter=limit=0.95` som siste ledd i SFX-syntese, og sjekk Peak level
  < -1 dB + lytt/mål i final-miksen.
- 2026-07-05: rgbashift tvinger YUV→RGB→YUV-konvertering på HELE strømmen
  (formatforhandling skjer uavhengig av enable) og taper farge overalt —
  bruk chromashift (nativ yuv420p) for kanalforskyvnings-effekter.
- 2026-07-05: ASCII-apostrof i drawtext-tekst kan ikke escapes robust
  gjennom både filtergraf- og drawtext-parsenivået — gamle \\\' knakk
  grafen så fort senere args hadde kommaer, og '\''-trikset spises av
  drawtext. Løsning: drawtext_escape bytter ' → typografisk ’.
- 2026-07-05: drawtext med expansion=normal (default) har INGEN fungerende
  escape for literal `%` — både `\%` og `%%` gir «Stray %» og teksten kan
  forsvinne helt. Regel: ALLE drawtext-kall skal ha `:expansion=none`
  (vi bruker aldri %{...}), og drawtext_escape skal ikke røre `%`.
- 2026-07-05: i ffmpeg `drawbox`-uttrykk betyr `t` TYKKELSE (thickness-
  parameteren), ikke tid — en "animert" drawbox står bom stille (og med
  t=fill blir uttrykket enormt). Tidsanimert posisjon krever `overlay` med
  t i x/y-uttrykk. Verifiser animasjoner med signalstats/YAVG per frame,
  ikke bare med å se på én frame.
- 2026-07-07: csdm sitt `framerate` er GLOBALT i video-configen — spec-ens
  antagelse om framerate per sekvens stemte ikke (verifisert i kilden,
  src/common/types/sequence.ts). P20 løses med ett render-pass per
  framerate (egen CS2-oppstart, ~2 min ekstra). Bonus-funn samme sted:
  sekvenser HAR et `cfg`-felt (per-sekvens console-kommandoer) — det er
  døråpneren P22 (in-game FOV-zoom) trenger.
- 2026-07-07: Effekter som legger innhold FORAN på tidslinja (cold open
  o.l.) må telles inn i beat-synkens startposisjon — glimtene lå utenfor
  regnskapet i to økter (all beat-sync ~0.75 s off med intro_hook på) før
  det ble oppdaget under P21-arbeidet.
