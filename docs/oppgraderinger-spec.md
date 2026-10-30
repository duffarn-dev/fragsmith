# Fragsmith — oppgraderingspakker (spec for Claude Code)

> Skrevet av prosjektrådgiver 2026-07-04. Dette er en **drøftings-/planleveranse**,
> ikke kode. Pakkene er sortert fra **lettest/raskest** øverst til **tyngst/lengst**
> nederst. Claude Code avgjør selv rekkefølge og hva som bygges først — men les
> "Fundament" og avhengighetene per pakke: flere effekter forutsetter P1.

## Slik brukes dokumentet
- Hver pakke er selvstendig og har: mål, avhengigheter, berørte filer, steg,
  akseptansekriterier, edge cases, estimert innsats.
- Alle brukervendte meldinger på norsk (jf. CLAUDE.md). Effekter styres via
  `config.toml` og skaleres av `[effects].intensity` der det gir mening.
- Ingen effekt skal svelge feil stille — logg via `run_ffmpeg` som i dag.
- Test alt som IKKE krever CS2 med mock-mp4-er. Alt som krever re-render
  (P2, P17, P19-P23) testes kun på render-PC-en.

## Relevant arkitektur (nåtilstand)
- Steg 6 `lib/edit_engine.py`: bygger `filter_complex` per segment
  (`_speed_parts`, `_effects_filters`, `_build_filter`). Her lever de fleste
  visuelle effektene.
- Steg 7 `lib/assemble.py`: intro/outro-kort (`_text_card`), concat, musikkmiks,
  `write_report`. Her lever intro/outro/struktur på filmnivå.
- Steg 4 render: `lib/render_clips.py` (bygger jobb) + `worker.py`
  (`build_csdm_config` — csdm/HLAE-sekvenser på render-PC-en).
- Modeller: `lib/models.py` (`Highlight`, `EditDecision`, `EditPlan`).
- Scoring: `lib/hype_score.py`. Director/LLM: `lib/director.py`.
- Felles ffmpeg: `lib/ffmpeg_util.py` (`run_ffmpeg`, `drawtext_escape`,
  `probe_duration`, `FONT_FILE`).

---

# FUNDAMENT (bygg først — låser opp mye)

## P1 — Per-kill-tidsstempler gjennom pipelinen
**Innsats:** Lav · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** Editing-motoren skal vite *hvor i klippet* hvert drap og headshot faller,
slik at effekter kan treffe selve drapsøyeblikket i stedet for å gjettes fra
klippstart.

**Berørte filer:** `lib/models.py`, `lib/hype_score.py`, `lib/parse_demo.py`
(kun hvis kill-felt mangler), `lib/director.py` (kontekst i prompt),
`lib/edit_engine.py` (konsumerer offsets).

**Steg:**
1. Legg et felt på `Highlight` i `models.py`, f.eks.
   `kills: list[KillMark]` der `KillMark` har `tick`, `offset_s`
   (sekunder fra klippets `start_tick`), `weapon`, `headshot: bool`,
   `victim_name`. Behold `schema_version`-mønsteret.
2. I `hype_score._clips_for_player` fyll `kills` fra `cluster` når klippet
   bygges (offset = `(kill_tick - start_tick) / tickrate`).
3. Gi Director kill-listen i prompten (`director._build_prompt`) som read-only
   kontekst — Director trenger ikke returnere den.
4. `edit_engine` leser `candidates_by_id[clip_id].kills` for effektplassering.

**Akseptansekriterier:**
- `01-candidates.json` inneholder `kills` med korrekte `offset_s` per klipp.
- `offset_s` ligger alltid innenfor `[0, duration_seconds]`.
- Ingen endring i eksisterende output (final.mp4 identisk hvis ingen effekt
  bruker feltet ennå).

**Edge cases:** speed-ramp/slow-mo endrer tidslinja i output — offsets er i
KILDE-tid; `edit_engine` må mappe kilde-offset → output-tid via `_speed_parts`
når en effekt skal plasseres. Dokumentér denne mappingen som hjelpefunksjon.

---

# TIER 1 — LAV INNSATS (quick wins)

## P2 — trueView + X-ray på render
**Innsats:** Lav · **Avhengigheter:** ingen · **Krever re-render:** ja (test)

**Mål:** Bedre POV-look (riktig kropp/armer) og "se fienden gjennom vegg"
fram til drap på utvalgte klipp.

**Berørte filer:** `worker.py` (`build_csdm_config`), `lib/render_clips.py`
(`build_render_job` — send flagg per klipp), `config.toml`.

**Steg:**
1. Legg `[render].true_view = true` i config; sett `trueView` i csdm-config.
2. Legg per-klipp `show_xray` i render-jobben (default: kun for `ace`/`clutch`
   og AWP-drap). Sett `showXRay` per sekvens i `build_csdm_config`.

**Akseptansekriterier:** csdm-config får riktige `trueView`/`showXRay`-verdier;
verifisert visuelt at sikte/killfeed ikke ødelegges av trueView.

**Edge cases:** X-ray på ALT ser rotete ut — hold det til høydepunkt-klipp.
trueView kan i sjeldne tilfeller skjule crosshair; verifiser per CS2-versjon
og noter i `learnings_runtime.md`.

## P3 — Eased speed-ramps (mykere slow inn/ut)
**Innsats:** Lav · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** Erstatt dagens brå faktorbytte inn/ut av slow-mo med en kort
overgangsrampe (ease), som ser mer proft ut.

**Berørte filer:** `lib/edit_engine.py` (`_speed_parts`, `_build_filter`).

**Steg:** ved grense mellom to hastigheter, sett inn 2-4 korte delsegmenter
med interpolert faktor (f.eks. 1.0 → 0.7 → 0.4) i stedet for direkte hopp.

**Akseptansekriterier:** ingen hørbart/synlig hakk ved slow-mo-start; total
segmentlengde uendret (beat-sync-logikken må fortsatt stemme).

**Edge cases:** `atempo` må kjedes riktig for mellomfaktorer (finnes alt i
`_atempo_chain`); korte ramper må ikke bli < ~2 frames.

## P4 — Freeze-frame på siste drap
**Innsats:** Lav · **Avhengigheter:** P1 (for treffsikker plassering) ·
**Krever re-render:** nei

**Mål:** Kort fryst bilde (0.3-0.6 s) på siste drap i en ace/høy multikill før
klippet slutter — gir et "punktum".

**Berørte filer:** `lib/edit_engine.py`.

**Steg:** på klipp der `kind in {ace, multikill(n>=4)}`, legg `tpad`/frame-hold
på siste kill-offset. Styr med `[effects].freeze_on_finish`.

**Akseptansekriterier:** freeze lander på drapet (± få frames); lengden telles
med i segmentets varighet så beat-sync holder.

**Edge cases:** freeze forlenger output — juster `target_duration`/beat-snap
tilsvarende, ellers glir hele filmen ut av synk.

## P5 — LUT-grading + film grain
**Innsats:** Lav · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** Konsistent filmisk look via `.cube`-LUT + valgfri film grain, i stedet
for kun `eq`/`vibrance`.

**Berørte filer:** `lib/edit_engine.py` (`_effects_filters`), `config.toml`,
ny mappe `assets/luts/`.

**Steg:**
1. `[effects].lut = "assets/luts/<navn>.cube"` (tom = av). Legg `lut3d`-filter
   i `_effects_filters` etter grade.
2. `[effects].grain = 0.0-1.0` → `noise=alls=<n>:allf=t`.
3. Valgfritt per-kart LUT via oppslag på `events["map"]`.

**Akseptansekriterier:** LUT lastes uten feil; grain skalerer med `intensity`;
av-verdi gir uendret bilde.

**Edge cases:** manglende `.cube`-fil skal gi tydelig flagg, ikke krasj.

## P6 — Letterbox på klimaks
**Innsats:** Lav · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** Cinematisk 2.35:1-letterbox som slår inn på slow-mo/clutch-klimaks som
visuell markør.

**Berørte filer:** `lib/edit_engine.py`.

**Steg:** `[effects].letterbox_on_slowmo`; legg svarte bånd (`drawbox` eller
`pad`/`crop`) med kort fade-in når segmentet har slow-mo eller `kind == clutch`.

**Akseptansekriterier:** båndene er symmetriske og forsvinner igjen; ikke aktiv
på vanlige klipp.

**Edge cases:** unngå at overlay-tekst havner bak båndene (juster y-posisjon).

---

# TIER 2 — LAV-MIDDELS INNSATS

## P7 — Kontekst-lowerthird
**Innsats:** Lav-middels · **Avhengigheter:** ingen (P1 gjør den bedre) ·
**Krever re-render:** nei

**Mål:** Diskret nedre info-linje (kart, runde, "1v4", score) så seeren skjønner
situasjonen — separat fra dagens store `overlay_text`.

**Berørte filer:** `lib/edit_engine.py`, `lib/models.py` (data finnes på
`Highlight`: `round`, `clutch_vs`, `weapons`), `config.toml`.

**Steg:** ny `drawtext`-linje nederst med mindre font, kort fade inn/ut i starten
av klippet. Bygg tekst fra `Highlight`-felt, ikke fra Director.

**Akseptansekriterier:** korrekt kontekst per klipp; ikke i konflikt med
`overlay_text` eller letterbox.

**Edge cases:** manglende felt (f.eks. ukjent kart) → hopp over linja, ikke
tom "None".

## P8 — Smooth slow-mo (minterpolate)
**Innsats:** Lav-middels · **Avhengigheter:** P3 anbefalt · **Krever re-render:** nei

**Mål:** Fjern hakking i slow-mo ved optical-flow-interpolasjon i stedet for
frame-duplisering (`setpts` alene).

**Berørte filer:** `lib/edit_engine.py`.

**Steg:** på slow-segmenter (`speed < 1.0`), kjør `minterpolate=fps=60:mi_mode=mci`
etter setpts. Styr med `[effects].smooth_slowmo` (kan slås av pga. encodetid).

**Akseptansekriterier:** synlig jevnere slow-mo på testklipp; encodetid logget.

**Edge cases:** `minterpolate` gir artefakter på røyk/molotov/partikler og er
tregt — bruk kun på slow-segmenter, ikke hele klippet. Noter artefakt-mønstre
i `learnings_runtime.md`.

## P9 — Animerte overlays
**Innsats:** Lav-middels · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** La `overlay_text` gli/fade inn (alpha- + y-uttrykk) i stedet for hard
på/av, for et mer "edit"-preg.

**Berørte filer:** `lib/edit_engine.py` (`drawtext`-blokken i `_build_filter`).

**Steg:** bruk tidsavhengig `alpha='...'` og evt. `y='...'` slide i `drawtext`;
behold `enable='between(...)'`-vinduet.

**Akseptansekriterier:** teksten animerer mykt; escaping via `drawtext_escape`
uendret; ingen syntaksfeil i filteruttrykket.

**Edge cases:** kompliserte uttrykk må quotes riktig i `filter_complex` — test
med `%`/`:`/`'` i teksten.

---

# TIER 3 — MIDDELS INNSATS

## P10 — Headshot zoom-punch + mikro-slowmo (signatur-effekt)
**Innsats:** Middels · **Avhengigheter:** P1 · **Krever re-render:** nei

**Mål:** "Zoom sakte inn, snap rask ut" på headshot, gjerne kombinert med et
kort slow-mo-øyeblikk — den effekten Petter etterspurte.

**Berørte filer:** `lib/edit_engine.py`, `config.toml`.

**Steg:**
1. `[effects].headshot_punch` + `..._strength`. For hver `KillMark` med
   `headshot=True` (default: siste/kraftigste i klippet), plasser på kill sin
   OUTPUT-tid (via P1-mappingen): kort ease-in zoom (~0.4 s til ~1.15×) +
   valgfri mikro-slowmo (factor ~0.4 i ~0.3 s), deretter snap tilbake på
   2-3 frames.
2. Sentrer zoom på biletsenter (POV = drapet er ~midt i bildet).

**Akseptansekriterier:** effekten treffer headshot-øyeblikket ± få frames;
skalerer med `intensity`; av-verdi = uendret.

**Edge cases:** flere headshots tett → ikke stable flere punch-er oppå hverandre
(sett min-avstand). Oppskalering ved zoom gir mildt kvalitetstap — hold zoom
moderat (< ~1.2×) uten P20.

## P11 — Animert kill-teller (1→2→3→ACE)
**Innsats:** Middels · **Avhengigheter:** P1 · **Krever re-render:** nei

**Mål:** Teller som ticker opp synket til hvert drap i en multikill/ace.

**Berørte filer:** `lib/edit_engine.py`, `config.toml`.

**Steg:** for klipp med `kill_count >= 3`, render `drawtext` som bytter tall på
hver `KillMark.offset_s` (output-tid); "ACE" på nr. 5. Kort pop-animasjon
(fontsize/alpha) per tikk.

**Akseptansekriterier:** tall bytter på riktige drap; siste = kill_count/"ACE".

**Edge cases:** samspill med `overlay_text` (unngå kollisjon i posisjon).

## P12 — RGB-split / impact-blink på drap
**Innsats:** Middels · **Avhengigheter:** P1 · **Krever re-render:** nei

**Mål:** Kort RGB-forskyvning eller blink på store drap for "impact".

**Berørte filer:** `lib/edit_engine.py`, `config.toml`.

**Steg:** `[effects].impact_split`; på utvalgte `KillMark` legg 1-2 frames
kanalforskyvning (`rgbashift`) eller kort hvit/svart blink.

**Akseptansekriterier:** kun på markerte drap; skalerer med `intensity`.

**Edge cases:** overdreven bruk = billig; begrens antall per film.

## P13 — Outro stats-kort
**Innsats:** Middels · **Avhengigheter:** ingen (P1 gir HS%) · **Krever re-render:** nei

**Mål:** Erstatt/utvid outro-tekstkortet med et stats-kort: K, K/D, HS%, beste
clutch, kart.

**Berørte filer:** `lib/assemble.py` (`_text_card` → nytt `_stats_card`),
`lib/hype_score.py` eller ny aggregator, `lib/models.py` (bær stats i `EditPlan`
eller egen struct), `config.toml`.

**Steg:**
1. Aggreger stats fra `events`/kandidater (totalt drap, HS%, høyeste clutch).
2. Ny `_stats_card` med flere linjer + enkel layout (fade som i dag).
3. Bruk den som outro når `[editing].stats_outro = true`.

**Akseptansekriterier:** tallene stemmer mot `report.md`; faller tilbake til
vanlig `outro_text` hvis stats mangler.

**Edge cases:** POV med få runder → ikke vis meningsløse prosenter.

## P14 — Intro-hook (teaser før tittel)
**Innsats:** Middels · **Avhengigheter:** P1 · **Krever re-render:** nei

**Mål:** 3-5 svært korte glimt (~0.15 s hver) av kommende drap før intro-kortet,
for umiddelbar hook.

**Berørte filer:** `lib/assemble.py`, `lib/edit_engine.py` (klipp ut korte
glimt), `config.toml`.

**Steg:** velg N høyest-scorende drap, klipp mikro-glimt rundt hver
`KillMark.offset_s`, sy sammen med harde kutt på beat før `00_intro`.

**Akseptansekriterier:** hooken ligger foran intro; total lengde logget; glimt
er hentet fra faktiske toppdrap.

**Edge cases:** ikke "spoil" hele filmen — bruk korte, uskarpe/raske glimt.

## P15 — SFX-lag (whoosh/impact)
**Innsats:** Middels · **Avhengigheter:** ingen (P1 for impact-treff) ·
**Krever re-render:** nei

**Mål:** Legg whoosh/riser på transitions og "impact" på store drap i lydmiksen.

**Berørte filer:** `lib/assemble.py` (miks), ny `assets/sfx/`, `config.toml`.

**Steg:** miks inn SFX-spor på transition-tider og på store `KillMark` (via P1),
under musikk men over game-audio i `amix`.

**Akseptansekriterier:** SFX hørbare men ikke dominerende; ingen klipping i
miksen (`normalize=0` finnes alt — pass på nivåer).

**Edge cases:** rettighetsfrie SFX i `assets/sfx/`; ikke hardkod stier.

## P16 — Dynamisk sidechain-ducking
**Innsats:** Middels · **Avhengigheter:** ingen · **Krever re-render:** nei

**Mål:** Erstatt fast `game_audio_volume` med `sidechaincompress` så game-audio
kun dukkes når musikken har transienter — renere miks.

**Berørte filer:** `lib/assemble.py`, `config.toml`.

**Steg:** bytt statisk `volume` mot `sidechaincompress` (musikk som sidekjede);
behold gammel modus som fallback via config-flagg.

**Akseptansekriterier:** game-audio pustes tydeligere; ingen pumpe-artefakter.

**Edge cases:** feil threshold/ratio gir pumping — eksponér parametre i config.

---

# TIER 4 — MIDDELS-HØY / HØY INNSATS

## P17 — Flere vinkler: POV-switch + dobbelrender
**Innsats:** Middels-høy · **Avhengigheter:** P1 · **Krever re-render:** ja

**Mål:** Andre vinkler enn ren egen-POV: bytt POV midt i klipp, og/eller render
samme drap fra offerets POV som eget klipp.

**Berørte filer:** `worker.py` (`build_csdm_config` — flere `playerCameras`
eller ekstra sekvens), `lib/render_clips.py` (`build_render_job`),
`lib/models.py` (angi ekstra vinkel-jobber), `config.toml`.

**Steg:**
1. Utvid render-jobbens klipp med valgfrie ekstra `playerCameras`-punkter
   (bytt til offer-steamid rett før drap).
2. Alternativ: legg en ekstra sekvens med samme tick-range men annen POV →
   nytt råklipp `<clip_id>_alt.mp4`.

**Akseptansekriterier:** csdm produserer forventede filer; `map_outputs` i
`worker.py` håndterer nye filnavn/nummer korrekt.

**Edge cases:** dobbelrender = mer render-tid og disk — flagg i fasestopp-
estimatet (`main.py`). Ikke gjør dette for alle klipp, kun utvalgte.

## P18 — Director-skjema: hybrid effektstyring
**Innsats:** Middels-høy · **Avhengigheter:** de effektene den skal styre ·
**Krever re-render:** nei

**Mål:** La deterministiske regler dekke defaults, men gi Director mulighet til
å OVERSTYRE/velge de store grepene (replay-klipp, klimaks-plassering,
letterbox, hvilke drap som får punch).

**Berørte filer:** `lib/models.py` (`EditDecision`-felt), `lib/director.py`
(prompt + validering `_semantic_errors`), `lib/edit_engine.py` (respekter felt).

**Steg:**
1. Legg valgfrie felt på `EditDecision`, f.eks. `effects: list[Literal[...]]`,
   `is_climax: bool`, `replay: bool`. Alle default av → deterministisk oppførsel
   uendret hvis Director utelater dem.
2. Utvid systemprompt med når effektene passer; utvid validering.

**Akseptansekriterier:** gamle planer (uten nye felt) fungerer uendret;
nye felt valideres; `report.md` viser Directors effektvalg.

**Edge cases:** hold LLM-scope smalt (jf. plan.md — LLM kun i steg 3). Ikke la
Director sette tallparametre for effekter, kun VELGE dem.

## P19 — Instant replay fra motsatt vinkel (flaggskip)
**Innsats:** Høy · **Avhengigheter:** P1, P17, gjerne P18 · **Krever re-render:** ja

**Mål:** Det sykeste drapet vises normal → kort rewind → slow-mo replay fra
offer-POV/annen vinkel. Signatur-moment.

**Berørte filer:** `lib/edit_engine.py` (replay-bygging), `lib/assemble.py`
(plassering), `worker.py`/`render_clips.py` (alt-vinkel-klipp fra P17),
`lib/models.py`/`director.py` (marker replay-klipp).

**Steg:** bygg sekvens: original-segment → reversert kort snutt (`reverse`) →
slow-mo av alt-vinkel-klippet, med "REPLAY"-overlay.

**Akseptansekriterier:** replay er tidsriktig mot beat; alt-vinkel matcher
samme drap; total lengde logget.

**Edge cases:** `reverse` er minnetungt på lange klipp — reverser kun en kort
snutt. Krever at alt-vinkel finnes (fallback: samme POV i slow-mo).

## P20 — Høy-fps render (ekte smooth slow-mo)
**Innsats:** Høy · **Avhengigheter:** slow-mo-beslutning FØR render ·
**Krever re-render:** ja

**Mål:** Render klipp som skal ha slow-mo i 120/240 fps i HLAE, så nedbremsing
blir ekte smooth (bedre enn P8 minterpolate).

**Berørte filer:** `worker.py` (`build_csdm_config` — `framerate` per sekvens),
`lib/render_clips.py` (send ønsket fps per klipp), `lib/director.py`/scoring
(slow-mo-beslutning må skje i steg 2-3, ikke steg 6), `config.toml`.

**Steg:**
1. Flytt "hvilke klipp får slow-mo"-avgjørelsen fram til før render (flytendring
   i pipelinen — dokumentér i CLAUDE.md).
2. Render de klippene i høy fps; `edit_engine` bremser med `setpts` uten
   interpolasjon.

**Akseptansekriterier:** slow-mo-segmenter er merkbart jevnere enn P8; render-tid
og diskestimat oppdatert i fasestopp.

**Edge cases:** mye større filer/lengre render — kun for utvalgte klipp. Bryter
dagens "alle effektvalg i steg 6"-antagelse; koordiner med P18.

## P21 — Bass-drop-koordinering
**Innsats:** Høy · **Avhengigheter:** P1, P18 · **Krever re-render:** nei

**Mål:** Plasser det sykeste klippet (eller drop-sekvensen) på musikkens drop, så
action og musikk topper sammen.

**Berørte filer:** `lib/beats.py` (drop/onset/energi-deteksjon), `lib/director.py`
(bruk drop-tid i rekkefølge), `lib/edit_engine.py`/`assemble.py`.

**Steg:** utvid `analyze_beats` med energibasert drop-deteksjon (librosa onset
strength / RMS-hopp); gi Director drop-tidspunkt; plasser klimaks der.

**Akseptansekriterier:** klimaks-klippet starter på/nær drop (± beat-toleranse).

**Edge cases:** spor uten tydelig drop → fall tilbake til dagens klimaks-til-
slutt-logikk.

## P22 — In-game FOV-zoom (optisk, ikke post)
**Innsats:** Høy · **Avhengigheter:** P1, csdm cfg-hooks · **Krever re-render:** ja

**Mål:** Ekte optisk zoom på drap via `fov`-kommando i sekvensen — skarpere enn
post-crop (P10).

**Berørte filer:** `worker.py` (`build_csdm_config` — cfg/kommandoer per tick,
MÅ verifiseres at csdm støtter dette), `lib/render_clips.py`, `config.toml`.

**Steg:** undersøk om csdm-sekvenser tar per-tick cfg/kommandoer; hvis ja, kjør
`fov`-endring rundt kill-tick.

**Akseptansekriterier:** zoom skjer i spillet, ingen oppskalering; verifisert på
render-PC.

**Edge cases:** hvis csdm ikke støtter per-tick cfg → droppes til fordel for P10.
Noter funn i `learnings_runtime.md`.

## P23 — Faste spektator-/overview-kameraer
**Innsats:** Høy · **Avhengigheter:** P1 · **Krever re-render:** ja

**Mål:** Etablerende faste kameraer (`cameras`-feltet, tomt i dag) på clutch-
runder for variasjon.

**Berørte filer:** `worker.py` (`build_csdm_config` — fyll `cameras`),
`lib/render_clips.py`, `config.toml`.

**Steg:** kartlegg hva csdm sitt `cameras`-felt faktisk tar (posisjon/vinkel?);
definer 1-2 faste vinkler per kart for etableringsskudd.

**Akseptansekriterier:** faste kamera-klipp produseres og er brukbare.

**Edge cases:** krever kart-spesifikke koordinater = manuelt arbeid per kart;
start med ett kart. Hvis for tungt → parkér.

---

# TIER 5 — SVÆRT HØY (utenfor nåværende scope)

## P24 — HLAE cinematic campath (flyvende kamera)
**Innsats:** Svært høy · **Status:** eksplisitt UTENFOR scope i `plan.md`

**Mål:** Ekte otv-fragmovie-look med flyvende, smoothede kameraer
(`mirv_campath`).

**Vurdering:** krever manuell keyframing per klipp og er skjørt mot spill-
oppdateringer — bryter "autonom uten manuelt arbeid"-målet. Tas kun opp igjen
hvis alt annet er ferdig OG det finnes en vei til å autogenerere campaths.
Ingen implementasjon nå; dokumentert for fullstendighet.

---

## Anbefalt lesning for prioritering
- Bygg **P1 først** uansett — P4, P10, P11, P12, P14, P17, P19, P20, P21, P22, P23
  bygger på den.
- Størst opplevd kvalitetshopp for minst risiko: **P8 + P10 + P3** (ingen
  re-render), deretter **P2** (nesten gratis når du uansett re-rendrer).
- P18 er limet mellom deterministiske effekter og regi — ta den når 3-4
  effektpakker finnes å styre.
