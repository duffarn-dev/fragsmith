"""Steg 3: AI Director — Claude velger endelige klipp, rekkefølge og pacing.

Eneste LLM-steget i pipelinen. Structured output via messages.parse() +
Pydantic (EditPlan). Ved validerings-/semantikkfeil: retry med feilmelding
tilbake til modellen, maks 3 forsøk, deretter DirectorError.

Alt logges: prompt til runs/<id>/02-edit-plan.prompt.txt, plan (inkl.
reasoning per klipp) til 02-edit-plan.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import anthropic

from lib.models import EditPlan, Highlight

MUSIC_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}


class DirectorError(RuntimeError):
    """Director klarte ikke produsere en gyldig plan."""


SYSTEM_PROMPT = """\
Du er AI Director for Fragsmith — en autonom fragmovie-generator for CS2.
Du får en liste kandidatklipp (med hype-score fra et regelbasert filter) og
skal lage en redigeringsplan for en fragmovie på vegne av spilleren.

Prinsipper for en god fragmovie:
- Start sterkt (hook), bygg mot det sykeste klippet mot slutten (klimaks).
- Varier pacing: ikke to lange klipp rett etter hverandre.
- Slow-motion brukes SPARSOMT — kun på det mest spektakulære øyeblikket i et
  klipp (siste kill i en ace, clutch-avslutningen). 0-2 segmenter per klipp.
- Dropp kjedelige kandidater uten dårlig samvittighet — 4 gode klipp slår
  8 middelmådige. Men: aces og høye clutcher (1v3+) skal praktisk talt alltid med.
- Overlay-tekst kort og på engelsk gaming-sjargong ("3K", "1v4 CLUTCH",
  "ACE") — null tekst på svakere klipp.
- transition_in: "cut" er standard; "fade" kun inn i tempo-skifte;
  "speed_ramp" der klippet starter i action.
- intro_text/outro_text: kort og stilrent, på engelsk (f.eks. spillernavn +
  kart, "GG" e.l.).

Effektstyring (P18 — valgfritt, per klipp):
- is_climax: sett på maks ETT klipp — filmens høydepunkt. Det klippet får
  ekstra tyngde (dutch tilt på siste kill). Utelater du det, regnes siste
  klipp som klimaks. Sett det når høydepunktet IKKE ligger sist.
- effects_on / effects_off: overstyr enkelteffekter der det løfter filmen:
  "letterbox" (cinematisk ramme — standard kun på slow-mo/clutch),
  "freeze" (freeze-frame på siste drap — standard kun på ace/4k),
  "kill_zoom" (dyp zoom på siste kill — standard på),
  "headshot_punch" (zoom-punch på headshots — standard på),
  "impact_split" (RGB-blink på store drap — standard på),
  "kill_counter" (kill-teller — standard på 3+ kills),
  "replay" (instant replay: siste drap spoles tilbake og vises i slow-mo
  fra OFFERETS synsvinkel — standard på klimaks-klippet hvis det har 3+
  kills eller clutch; maks ETT klipp kan ha replay i effects_on).
  Standardreglene er gode: bruk dette KUN med en konkret grunn (f.eks.
  freeze på en 3k som avslutter filmen, letterbox av på et hektisk klipp,
  replay på filmens sykeste enkeltdrap). Aldri tallparametre — kun valg.

Begrunn hvert valg kort i reasoning-feltet (norsk er fint der).
"""


def _build_prompt(
    candidates: list[Highlight],
    events: dict,
    music_files: list[str],
    pov_name: str,
) -> str:
    cand_json = json.dumps(
        [c.model_dump() for c in candidates], indent=2, ensure_ascii=False
    )
    rounds = events.get("rounds", [])
    return f"""\
Lag en redigeringsplan (EditPlan) for denne demoen.

Kontekst:
- Spiller (POV): {pov_name}
- Kart: {events.get('map') or 'ukjent'}
- Antall runder: {len(rounds)}
- Demo: {events.get('demo_path')}

Kandidatklipp (fra regelbasert hype-scoring, 0-10):
{cand_json}

Tilgjengelige musikkspor (velg ETT eksakt filnavn til music_track):
{json.dumps(music_files, indent=2, ensure_ascii=False)}

Krav:
- Hver kandidat skal ENTEN ha en EditDecision ELLER stå i dropped_clip_ids.
- order er 1-indeksert og sammenhengende (1, 2, 3, ...) i visningsrekkefølge.
- slowmo_segments er i sekunder relativt til klippets start og må ligge
  innenfor klippets varighet (duration_seconds). factor 0.2-0.5 for slow-mo.
- kills[] per kandidat er read-only kontekst: hvert drap med offset_s
  (sekunder fra klippstart), våpen, headshot og offer. Bruk dem til å legge
  slowmo_segments PRESIST rundt det mest spektakulære drapet (typisk siste)
  i stedet for å gjette — f.eks. fra ~0.5 s før til ~0.5 s etter offset_s.
- clutch_vs=1 betyr en VUNNET 1v1-DUELL: legg alltid et kort slow-mo
  (factor ~0.4) rundt siste kill i det klippet — duellavslutningen er poenget.
- Har FLERE kandidater i samme runde clutch_vs >= 2, er de deler av SAMME
  clutch (lang pause mellom kills splitter klipp). Da skal ALLE med, rett
  etter hverandre i kronologisk rekkefølge (stigende start_tick) — det siste
  klippet er selve payoffen og droppes ALDRI. Slow-mo hører hjemme på
  avslutningskillet i SISTE del.
- demo_path settes til demoens sti (over).
- is_climax/effects_on/effects_off er valgfrie — utelat dem der standard-
  reglene holder. Samme effekt kan ikke stå i både effects_on og effects_off.
"""


def _semantic_errors(
    plan: EditPlan, candidates: list[Highlight], music_files: list[str]
) -> list[str]:
    errors: list[str] = []
    by_id = {c.clip_id: c for c in candidates}

    used = [d.clip_id for d in plan.decisions]
    unknown = [c for c in used + plan.dropped_clip_ids if c not in by_id]
    if unknown:
        errors.append(f"Ukjente clip_id-er: {unknown}. Gyldige: {sorted(by_id)}")
    if len(set(used)) != len(used):
        errors.append("Duplikate clip_id-er i decisions.")

    covered = set(used) | set(plan.dropped_clip_ids)
    missing = sorted(set(by_id) - covered)
    if missing:
        errors.append(
            f"Kandidater verken valgt eller droppet: {missing}. "
            f"Alle skal enten ha en decision eller stå i dropped_clip_ids."
        )

    orders = sorted(d.order for d in plan.decisions)
    if orders != list(range(1, len(orders) + 1)):
        errors.append(f"order må være 1..{len(orders)} uten hull, var: {orders}")

    for d in plan.decisions:
        cand = by_id.get(d.clip_id)
        if not cand:
            continue
        for seg in d.slowmo_segments:
            if not (0 <= seg.start_s < seg.end_s <= cand.duration_seconds + 0.5):
                errors.append(
                    f"{d.clip_id}: slowmo {seg.start_s}-{seg.end_s}s utenfor "
                    f"klippets varighet ({cand.duration_seconds}s)."
                )
            if not (0.1 <= seg.factor <= 1.0):
                errors.append(
                    f"{d.clip_id}: slowmo factor {seg.factor} utenfor 0.1-1.0."
                )

    if music_files and plan.music_track not in music_files:
        errors.append(
            f"music_track '{plan.music_track}' finnes ikke. Velg fra lista."
        )

    # P18: maks ett klimaks-klipp; on/off kan ikke overlappe
    climax = [d.clip_id for d in plan.decisions if d.is_climax]
    if len(climax) > 1:
        errors.append(
            f"is_climax er satt på {len(climax)} klipp ({climax}) — maks ett."
        )
    for d in plan.decisions:
        both = set(d.effects_on) & set(d.effects_off)
        if both:
            errors.append(
                f"{d.clip_id}: {sorted(both)} står i både effects_on og "
                f"effects_off — velg én."
            )
    replay_on = [d.clip_id for d in plan.decisions if "replay" in d.effects_on]
    if len(replay_on) > 1:
        errors.append(
            f"'replay' i effects_on på {len(replay_on)} klipp ({replay_on}) "
            f"— maks ett (én replay per film)."
        )

    # Delt clutch (flere klipp fra samme vunnede clutch-runde): alle må med,
    # sammenhengende og i kronologisk rekkefølge — payoffen ligger i siste.
    clutch_rounds: dict[int, list] = {}
    for c in candidates:
        if c.clutch_vs and c.clutch_vs >= 2:
            clutch_rounds.setdefault(c.round, []).append(c)
    order_by_id = {d.clip_id: d.order for d in plan.decisions}
    for rnd_no, group in clutch_rounds.items():
        if len(group) < 2:
            continue
        ids = [c.clip_id for c in sorted(group, key=lambda c: c.start_tick)]
        if any(cid not in order_by_id for cid in ids):
            errors.append(
                f"Runde {rnd_no}: clutchen er delt i flere klipp ({ids}) — "
                f"ALLE må inkluderes; siste klipp er payoffen."
            )
            continue
        orders = [order_by_id[cid] for cid in ids]
        if orders != list(range(min(orders), min(orders) + len(orders))):
            errors.append(
                f"Runde {rnd_no}: clutch-delene {ids} må stå rett etter "
                f"hverandre i kronologisk rekkefølge (order {orders})."
            )
    return errors


def list_music_files(music_dir: str | Path) -> list[str]:
    music_dir = Path(music_dir)
    if not music_dir.is_dir():
        return []
    return sorted(
        f.name for f in music_dir.iterdir() if f.suffix.lower() in MUSIC_EXTENSIONS
    )


def run_director(
    candidates: list[Highlight],
    events: dict,
    music_files: list[str],
    pov_name: str,
    run_dir: Path,
    model: str,
    max_retries: int = 3,
) -> EditPlan:
    client = anthropic.Anthropic()
    prompt = _build_prompt(candidates, events, music_files, pov_name)
    (run_dir / "02-edit-plan.prompt.txt").write_text(prompt, encoding="utf-8")

    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=messages,
            output_format=EditPlan,
        )
        if response.stop_reason == "refusal":
            raise DirectorError("Modellen avslo forespørselen (refusal).")

        plan = response.parsed_output
        if plan is None:
            last_errors = ["Svaret kunne ikke parses til EditPlan."]
        else:
            last_errors = _semantic_errors(plan, candidates, music_files)
            if not last_errors:
                plan.demo_path = events.get("demo_path", plan.demo_path)
                return plan

        raw_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
        messages.append({"role": "assistant", "content": raw_text or "(tomt)"})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Planen din hadde feil — rett dem og returner hele planen "
                    "på nytt:\n- " + "\n- ".join(last_errors)
                ),
            }
        )
        print(f"    Director-forsøk {attempt} feilet: {len(last_errors)} feil")

    raise DirectorError(
        f"Ga opp etter {max_retries} forsøk. Siste feil: {last_errors}"
    )
