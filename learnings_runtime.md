# Learnings — runtime (fragsmith)

Mønstre fra faktiske kjøringer. main.py leser denne som STEG 0, før alt annet.
Agenten foreslår tillegg etter hver kjøring; brukeren godkjenner.
Format: `- YYYY-MM-DD: <hva> — <konsekvens/regel>`

## Demo-kilder (Valve MM vs FACEIT, tick-oppførsel, subtick)
- 2026-07-03: CS2 nekter avspilling av demoer fra eldre spillversjoner
  ("Demo file incompatible with this game version"). Demoer må rendres mens
  de er ferske (samme/nær spillversjon) — gamle arkiver kan kun parses/
  analyseres (steg 1-3), aldri filmes. Verifisert med juni 2024-demo mot
  juli 2026-klient. Ferske demoer skaffes via egne matcher, csstats.gg
  (nylige matcher), share-koder (`csdm dl-valve <kode>`) eller `dl-faceit`.
- 2026-07-03: csdm `analyze` kan klage på gamle demoer ("unknown grenade
  model") og `csdm video` returnerer exit 0 selv når CS2-avspillingen feiler
  — antall produserte output-filer er eneste pålitelige suksess-signal
  (worker sjekker dette og rapporterer status feil/delvis/ok).

## HLAE-brekk (spilloppdatering → fiks)
(ingen ennå)

## Render-innstillinger (trueView, X-ray)
- 2026-07-07: P20 verifisert med ekte rendering (de_overpass
  re-rendret): to csdm-pass per jobb fungerer i praksis (~1 min ekstra per
  ekstra pass for CS2-oppstart); 120 fps-råklipp er ~2× størrelse. Slow-mo-
  kvalitet: 0 duplikatframes, minterpolate ute av bildet for høy-fps-kilder.
- 2026-07-05: trueView + showXRay verifisert OK på CS2-versjonen per juli
  2026 — crosshair, killfeed og X-ray-silhuetter intakte (de_inferno).
  Re-verifiser etter spilloppdateringer.

## Hype-scoring (kjedelige klipp valgt / gems oversett → regeljusteringer)
- 2026-07-03: Stille clutcher (vunnet uten kills i clutch-fasen, f.eks. på tid/
  defuse) gir clutch-flagg på tidligere kills i runden — klippet dekker da ikke
  selve clutch-øyeblikket. Vurder å utvide clutch-klipp til rundeslutt, eller
  kreve minst 1 kill etter at clutch-situasjonen oppsto. (Sett i runde 7,
  de_anubis.) — FIKSET 2026-07-05: detect_won_clutches
  returnerer nå fra_tick; kun klipp som slutter etter det får flagget.

## Konfig & kommandoflyt
- 2026-07-05: `finish` uten `--music` bruker Directors valg fra
  02-edit-plan.json — ble filmen originalt laget med `--music`-overstyring,
  gir re-kjøring stille en annen lydside. Oppgi `--music` eksplisitt ved
  re-kjøringer (eller lagre overstyringen i planen).
- 2026-07-05: known_players i config.toml var byttet til steamids som ikke
  finnes i noen demo (feil-oppløste vanity-URLer) — POV i alle leverte filmer
  var en annen konto. Regel: verifiser alltid ny kontoliste
  mot en faktisk demo før den tas i bruk. Venne-filmer kjøres med
  `--player <steamid64>`, ikke ved å utvide known_players.

## Musikk & beat-sync (P21)
- 2026-07-07: P21-drop-deteksjon verifisert på suno-spor (Sharp Shooter.wav):
  fant 5 kandidater, sterkeste var et ekte breakdown→drop (+10 dB); klimaks
  landet +8 ms fra droppet i levert film (de_overpass). NB: sterkeste-
  drop-prioriteringen kan velge musikk-offset (hopper over sporets intro)
  selv når et svakere drop var nåelig med intro-spaken — filmen ble vurdert
  og var fornøyd, så prioriteringen står; revurder hvis et innhopp midt i
  en låt låter brått på et annet spor.

## ffmpeg (filterkjeder som ga artefakter)
(ingen ennå)
