"""Steg 6: Editing engine — slow-mo/speed-ramps, overlays, beat-justerte
klippelengder. Rå ffmpeg via subprocess (bevisst valg fremfor moviepy).

Input:  raw clips (03-raw-clips/<clip_id>.mp4) + edit-plan + beats
Output: 04-segments/NN_<clip_id>.mp4 (uniform 1080p60 h264 + 48k stereo)
        + cut-logg med beat-avvik per kutt (kravet: ±100 ms verifiseres
        med øret; tallene logges så de kan sammenlignes)
"""
from __future__ import annotations

import math
from pathlib import Path

from lib.beats import nearest_beat_before, shift_beats
from lib.ffmpeg_util import (
    FONT_FILE,
    drawtext_escape,
    probe_duration,
    probe_fps,
    run_ffmpeg,
)
from lib.models import EditDecision, EditPlan

ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-r", "60",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
]


def _atempo_chain(speed: float) -> str:
    """atempo støtter 0.5-2.0 per ledd — kjed for lavere/høyere faktorer."""
    parts: list[float] = []
    remaining = speed
    while remaining < 0.5:
        parts.append(0.5)
        remaining /= 0.5
    while remaining > 2.0:
        parts.append(2.0)
        remaining /= 2.0
    parts.append(remaining)
    return ",".join(f"atempo={p:.4f}" for p in parts)


def _speed_parts(
    duration: float,
    decision: EditDecision,
    extra_slow: list[tuple[float, float, float]] = (),
) -> list[tuple[float, float, float]]:
    """Del klippet i (start, slutt, hastighet)-deler som dekker hele varigheten.

    extra_slow: ekstra (start, slutt, faktor)-markører utenom Directors plan —
    brukes av P10 mikro-slowmo på headshots.
    """
    markers: list[tuple[float, float, float]] = []
    if decision.transition_in == "speed_ramp":
        markers.append((0.0, min(0.6, duration * 0.2), 1.7))
    for seg in decision.slowmo_segments:
        start = max(0.0, min(seg.start_s, duration))
        end = max(start, min(seg.end_s, duration))
        if end - start > 0.05:
            markers.append((start, end, seg.factor))
    for start, end, factor in extra_slow:
        start = max(0.0, min(start, duration))
        end = max(start, min(end, duration))
        if end - start > 0.05:
            markers.append((start, end, factor))
    markers.sort()

    parts: list[tuple[float, float, float]] = []
    cursor = 0.0
    for start, end, speed in markers:
        start = max(start, cursor)  # overlapp: senere segment vinner ikke bakover
        if start >= end:
            continue
        if start > cursor:
            parts.append((cursor, start, 1.0))
        parts.append((start, end, speed))
        cursor = end
    if cursor < duration:
        parts.append((cursor, duration, 1.0))
    return parts


def source_to_output_time(
    t: float, parts: list[tuple[float, float, float]]
) -> float:
    """P1: mapp et tidspunkt i KILDE-klippet til tid i output-tidslinja.

    KillMark.offset_s er i kilde-tid; slow-mo/ramper strekker tidslinja, så
    et drap på 5.0 s i kilden lander senere i output når noe før det går i
    slow-mo. Effekter som skal treffe et drap (P4/P10/P11/P12) MÅ mappe
    offset gjennom denne, med de samme parts som _build_filter fikk.
    """
    out = 0.0
    for start, end, speed in parts:
        if t <= start:
            break
        out += (min(t, end) - start) / speed
        if t <= end:
            break
    return out


def _split_parts_at(
    parts: list[tuple[float, float, float]], t: float
) -> tuple[list[tuple[float, float, float]], int]:
    """P4: del parts ved kilde-tid t og returner (parts, freeze-innsettingsindeks).

    Indeksen er hvor mange deler som ligger FØR t — freeze-frame-kjeden i
    _build_filter settes inn der. Ligger t < 0.02 s fra en eksisterende
    grense splittes ikke (delsegmenter under ~1 frame er ubrukelige).
    """
    out: list[tuple[float, float, float]] = []
    for start, end, speed in parts:
        if start + 0.02 < t < end - 0.02:
            out.append((start, t, speed))
            out.append((t, end, speed))
        else:
            out.append((start, end, speed))
    idx = sum(1 for _, end, _ in out if end <= t + 0.021)
    return out, idx


RAMP_STEPS = 3      # delsegmenter per hastighetsovergang (spec P3: 2-4)
RAMP_HALF_S = 0.15  # kilde-tid som stjeles fra hver side av grensa
MIN_STEP_S = 0.04   # rampesteg må ikke bli < ~2 frames ved 60 fps


def _eased_parts(
    parts: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """P3: myke speed-ramps — erstatt harde hastighetshopp med korte
    interpolerte rampesteg rundt grensa (f.eks. 1.0 → 0.85 → 0.7 → 0.55 → 0.4).

    Stjeler inntil RAMP_HALF_S fra hver side (maks 40 % av nabodelens lengde).
    Blir stegene for korte (< MIN_STEP_S) beholdes grensa hard.
    """
    if len(parts) < 2:
        return parts
    mutable = [list(p) for p in parts]
    ramps: dict[int, list[tuple[float, float, float]]] = {}
    for j in range(len(mutable) - 1):
        left, right = mutable[j], mutable[j + 1]
        s1, s2 = left[2], right[2]
        if abs(s1 - s2) < 0.05:
            continue
        half = min(
            RAMP_HALF_S, 0.4 * (left[1] - left[0]), 0.4 * (right[1] - right[0])
        )
        step = 2 * half / RAMP_STEPS
        if step < MIN_STEP_S:
            continue
        boundary = left[1]
        left[1] = boundary - half
        right[0] = boundary + half
        ramps[j] = [
            (
                boundary - half + k * step,
                boundary - half + (k + 1) * step,
                s1 + (s2 - s1) * (k + 1) / (RAMP_STEPS + 1),
            )
            for k in range(RAMP_STEPS)
        ]
    eased: list[tuple[float, float, float]] = []
    for j, p in enumerate(mutable):
        if p[1] - p[0] > 1e-6:
            eased.append((p[0], p[1], p[2]))
        if j in ramps:
            eased.extend(ramps[j])
    return eased


def _effects_filters(
    effects: dict, beat_ctx: dict | None
) -> list[str]:
    """Effektkjede (før tekst-overlay): grade, beat-zoom, wobble, vignett.

    Alt skaleres av [effects].intensity. Beat-zoom bruker musikkens faktiske
    BPM/fase så pulsen treffer beatene.
    """
    if not effects.get("enabled", True):
        return []
    k = max(0.0, float(effects.get("intensity", 1.0)))
    vf: list[str] = []

    if effects.get("grade", True) and k > 0:
        vf.append(
            f"eq=contrast={1 + 0.05 * k:.3f}:saturation={1 + 0.22 * k:.3f}"
        )
        vf.append(f"vibrance=intensity={0.25 * k:.3f}")
        vf.append(f"unsharp=5:5:{0.3 * k:.2f}")

    # P5: LUT-grading (.cube) — eksistens sjekkes i build_segments (flagg)
    lut = str(effects.get("lut", "") or "")
    if lut and k > 0:
        lut_path = Path(lut).as_posix().replace(":", "\\:")
        vf.append(f"lut3d=file='{lut_path}'")

    # P5: film grain — temporal støy, skalert av intensity
    grain = max(0.0, float(effects.get("grain", 0.0)))
    if grain > 0 and k > 0:
        vf.append(f"noise=alls={min(100.0, grain * 12 * k):.1f}:allf=t")

    if effects.get("beat_zoom", True) and beat_ctx and k > 0:
        amp = 0.035 * k * float(effects.get("beat_zoom_strength", 1.0))
        p = beat_ctx["period"]
        ph = beat_ctx["phase"]
        # tid siden forrige beat: u = mod(t - fase + P, P); zoom-puls som
        # slår til på beatet og dør ut eksponentielt
        expr = (
            f"1+{amp:.4f}*exp(-7*mod(on/60-{ph:.3f}+{p:.4f},{p:.4f})/{p:.4f})"
        )
        vf.append(
            f"zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:fps=60:s=1920x1080"
        )

    if effects.get("wobble", False) and k > 0:
        period4 = (beat_ctx["period"] * 4) if beat_ctx else 2.0
        vf.append(
            f"rotate=a='{0.012 * k:.4f}*sin(2*PI*t/{period4:.3f})':ow=iw:oh=ih:c=black"
        )

    if effects.get("vignette", True) and k > 0:
        vf.append("vignette=PI/5")
    return vf


def _max_tilt_degrees(zoom: float) -> float:
    """Største tilt-vinkel der zoom-beskjæringen dekker de svarte hjørnene
    fra rotate (16:9): cos(θ) + (W/H)·sin(θ) <= zoom, minus 1° margin."""
    ratio = 1920 / 1080
    r = math.hypot(1.0, ratio)
    if zoom >= r:
        return 45.0
    return max(0.0, math.degrees(math.atan(ratio) - math.acos(zoom / r)) - 1.0)


def _kill_punch_filters(kill_fx: dict, effects: dict) -> list[str]:
    """Dyp zoom inn på crosshair (bildesenter) på kill-tidspunktet, valgfritt
    kombinert med dutch tilt. Tider er i OUTPUT-tid (via source_to_output_time).

    Konvolutt: zoom rammer inn drapet — full ~0.05 s FØR kill, holdes ~0.4 s,
    snapper ut på ~0.18 s. Tilten følger zoomen med brattere kurve (^3.5 mot
    ^2) så beskjæringen alltid dekker rotasjons-hjørnene underveis.
    """
    t_kill = kill_fx["t"]
    zmax = max(1.1, float(effects.get("kill_zoom_factor", 1.7)))
    dz = zmax - 1.0
    # P4: faller en freeze-frame på samme kill holdes zoomen gjennom hele
    # frysen (hold_until) før snap-out — ellers standard 0.4 s hold
    t0, t1 = t_kill - 0.40, t_kill - 0.05
    t2 = float(kill_fx.get("hold_until", t_kill + 0.40))
    t3 = t2 + 0.18

    def z_expr(tv: str) -> str:
        return (
            f"if(lt({tv},{t0:.3f}),1,"
            f"if(lt({tv},{t1:.3f}),1+{dz:.4f}*pow(({tv}-{t0:.3f})/0.35,2),"
            f"if(lt({tv},{t2:.3f}),{zmax:.4f},"
            f"if(lt({tv},{t3:.3f}),{zmax:.4f}-{dz:.4f}*({tv}-{t2:.3f})/0.18,1))))"
        )

    vf: list[str] = []
    deg = min(float(kill_fx.get("tilt_deg", 0.0)), _max_tilt_degrees(zmax))
    if deg > 0:
        rad = math.radians(deg)
        a = (
            f"if(lt(t,{t0:.3f}),0,"
            f"if(lt(t,{t1:.3f}),{rad:.5f}*pow((t-{t0:.3f})/0.35,3.5),"
            f"if(lt(t,{t2:.3f}),{rad:.5f},"
            f"if(lt(t,{t3:.3f}),{rad:.5f}*pow(1-(t-{t2:.3f})/0.18,2.5),0))))"
        )
        vf.append(f"rotate=a='{a}':ow=iw:oh=ih:c=black")
    vf.append(
        f"zoompan=z='{z_expr('on/60')}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=60:s=1920x1080"
    )
    return vf


def _headshot_punch_filter(punches: list[dict], _effects: dict) -> str:
    """P10: liten zoom-punch på headshots — ease-in ~0.35 s til maks (< ~1.25×),
    kort hold over drapet, snap ut på 2-3 frames. Alle punch-er samles i ÉN
    zoompan (hver konvolutt er 0 utenfor sitt vindu), sentrert på crosshair.
    Tider er i OUTPUT-tid (via source_to_output_time + evt. freeze-skift)."""
    tv = "on/60"
    envs: list[str] = []
    for p in punches:
        tk, dz = p["t"], p["dz"]
        t0, t1, t2, t3 = max(0.0, tk - 0.40), tk - 0.05, tk + 0.12, tk + 0.16
        envs.append(
            f"if(lt({tv},{t0:.3f}),0,"
            f"if(lt({tv},{t1:.3f}),{dz:.4f}*pow(({tv}-{t0:.3f})/{t1 - t0:.3f},2),"
            f"if(lt({tv},{t2:.3f}),{dz:.4f},"
            f"if(lt({tv},{t3:.3f}),{dz:.4f}*(1-({tv}-{t2:.3f})/0.04),0))))"
        )
    z = "1+" + "+".join(envs)
    return (
        f"zoompan=z='{z}'"
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=60:s=1920x1080"
    )


def _build_filter(
    parts: list[tuple[float, float, float]],
    decision: EditDecision,
    effects: dict,
    beat_ctx: dict | None,
    letterbox: bool = False,
    kill_fx: dict | None = None,
    freeze: dict | None = None,
    hs_punches: list[dict] | None = None,
    punch_skip: list[tuple[float, float]] = (),
    counter: list[tuple[float, str]] | None = None,
    lowerthird: str | None = None,
    impacts: list[float] | None = None,
    src_fps: float = 60.0,
) -> tuple[str, str, str]:
    """filter_complex: trim/setpts/atempo per del + concat + effekter +
    overlay + inngangsovergang. src_fps: kildens framerate (P20) — høy-fps-
    kilder bremses med ekte frames, uten minterpolate."""
    punch = effects.get("enabled", True) and effects.get("slowmo_punch", True)
    smooth = (
        effects.get("enabled", True)
        and effects.get("smooth_slowmo", False)
        and src_fps <= HIGHFPS_THRESHOLD  # P20: ekte frames slår interpolasjon
    )
    k = max(0.0, float(effects.get("intensity", 1.0)))

    chains: list[str] = []
    labels: list[tuple[str, str]] = []  # (video, audio) i concat-rekkefølge
    for i, (start, end, speed) in enumerate(parts):
        vchain = (
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=(PTS-STARTPTS)/{speed:.4f}"
        )
        if smooth and speed < 0.95:
            # P8: optical flow-interpolasjon → jevn slow-mo i stedet for
            # frame-duplisering. Kun på slow-deler — minterpolate er treg
            # og gir artefakter på røyk/partikler (se learnings_runtime.md).
            vchain += ",minterpolate=fps=60:mi_mode=mci"
        # normaliser oppløsning OG fps FØR concat — punch-zoompan låser sin
        # del til 1080p, concat krever samme størrelse, og alle zoompan-
        # uttrykk etter concat regner tid som on/60 (kilder ≠ 60 fps ville
        # ellers fått effektene på halv/dobbel tid)
        vchain += (
            ",fps=60,scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
        )
        # P10: mikro-slowmo-deler har egen headshot-punch — push-in oppå
        # ville stablet to zoomer på samme drap
        in_micro = any(
            start >= ms - 1e-3 and end <= me + 1e-3 for ms, me in punch_skip
        )
        # lengdekravet holder push-in unna korte rampesteg fra _eased_parts
        # (zoom som restarter per steg ville gitt synlig rykk)
        if punch and speed < 1.0 and k > 0 and (end - start) >= 0.25 and not in_micro:
            # push-in under slow-mo: sakte zoom fra 1.02 til ~1.02+0.12*k
            out_dur = max(0.1, (end - start) / speed)
            rate = (0.12 * k) / (out_dur * 60)
            vchain += (
                f",zoompan=z='1.02+{rate:.6f}*on'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:fps=60:s=1920x1080"
            )
        chains.append(f"{vchain}[v{i}]")
        chains.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"{_atempo_chain(speed)}[a{i}]"
        )
        labels.append((f"[v{i}]", f"[a{i}]"))

    if freeze:
        # P4: freeze-frame — ta én frame på kill-tidspunktet, klon den i
        # freeze-varigheten (tpad), stum lyd under. Settes inn i concat-rekka
        # på after_idx (fra _split_parts_at) så den lander PÅ drapet.
        ft, fdur = freeze["src_t"], freeze["dur"]
        chains.append(
            f"[0:v]trim=start={ft:.3f}:end={ft + 0.1:.3f},setpts=PTS-STARTPTS,"
            f"select='eq(n,0)',tpad=stop_mode=clone:stop_duration={fdur:.3f},"
            f"fps=60,scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2[vfz]"
        )
        chains.append(
            f"aevalsrc=0|0:s=48000:d={fdur:.3f},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[afz]"
        )
        labels.insert(int(freeze["after_idx"]), ("[vfz]", "[afz]"))

    inputs = "".join(v + a for v, a in labels)
    chains.append(f"{inputs}concat=n={len(labels)}:v=1:a=1[vc][ac]")

    # (oppløsning er allerede normalisert per del, før concat)
    vf = _effects_filters(effects, beat_ctx)
    if hs_punches:
        # P10: headshot-punch før kill-punch — begge er senter-zoom, og
        # utvalget i build_segments sikrer at vinduene ikke overlapper
        vf.append(_headshot_punch_filter(hs_punches, effects))
    if kill_fx:
        # kill-punch FØR tekst/letterbox: teksten skal ikke rotere med,
        # og letterbox-båndene skal ligge i ro over det tiltede bildet
        vf += _kill_punch_filters(kill_fx, effects)
    if impacts:
        # P12: 3 frames kroma-forskyvning på store drap — "impact".
        # chromashift, IKKE rgbashift: rgbashift tvinger YUV→RGB→YUV på HELE
        # klippet (formatforhandling skjer uavhengig av enable) og taper
        # farge overalt; chromashift jobber i nativ yuv420p. Etter kill-punch
        # (forskyver det zoomede bildet), før tekstlagene.
        shift = max(2, round(5 * k))
        cond = "+".join(f"between(t,{t:.3f},{t + 0.05:.3f})" for t in impacts)
        vf.append(f"chromashift=cbh=-{shift}:crh={shift}:enable='{cond}'")
    if decision.overlay_text:
        text = drawtext_escape(decision.overlay_text)
        if effects.get("enabled", True) and effects.get("animated_overlays", False):
            # P9: teksten glir opp og fader inn (0.4-0.65 s), fader ut mot
            # 3.2 s — i stedet for hard på/av
            alpha = (
                "if(lt(t,0.4),0,if(lt(t,0.65),(t-0.4)/0.25,"
                "if(lt(t,2.9),1,max(0,(3.2-t)/0.3))))"
            )
            y = "h*0.72+h*0.03*pow(max(0,1-(t-0.4)/0.25),2)"
            vf.append(
                f"drawtext=fontfile='{FONT_FILE}':text='{text}':expansion=none"
                f":fontsize=88:fontcolor=white:borderw=5:bordercolor=black"
                f":x=(w-text_w)/2:y='{y}'"
                f":enable='between(t,0.4,3.2)':alpha='{alpha}'"
            )
        else:
            vf.append(
                f"drawtext=fontfile='{FONT_FILE}':text='{text}':expansion=none"
                f":fontsize=88:fontcolor=white:borderw=5:bordercolor=black"
                f":x=(w-text_w)/2:y=h*0.72"
                f":enable='between(t,0.4,3.2)'"
            )
    if counter:
        # P11: kill-teller som ticker opp på hvert drap (output-tid) med
        # alpha-pop per tikk. Venstre side — kolliderer ikke med killfeed
        # (topp høyre), overlay_text (senter) eller lowerthird (nede venstre).
        for i, (start, label) in enumerate(counter):
            end = counter[i + 1][0] if i + 1 < len(counter) else 9999.0
            vf.append(
                f"drawtext=fontfile='{FONT_FILE}'"
                f":text='{drawtext_escape(label)}':expansion=none"
                f":fontsize=110:fontcolor=white:borderw=6:bordercolor=black"
                f":x=70:y=h*0.35"
                f":enable='between(t,{start:.3f},{end:.3f})'"
                f":alpha='min(1,(t-{start:.3f})/0.15)'"
            )
    if lowerthird:
        # P7: diskret kontekstlinje (kart · runde · 1vX) nede til venstre,
        # kort fade inn/ut. Løftes over bunn-båndet når letterbox er aktiv.
        y = 870 if letterbox else 1010
        vf.append(
            f"drawtext=fontfile='{FONT_FILE}'"
            f":text='{drawtext_escape(lowerthird)}':expansion=none"
            f":fontsize=34:fontcolor=white:borderw=3:bordercolor=black"
            f":x=70:y={y}"
            f":enable='between(t,0.5,4.5)'"
            f":alpha='if(lt(t,0.8),(t-0.5)/0.3,if(lt(t,4.2),1,(4.5-t)/0.3))'"
        )
    if decision.transition_in == "fade":
        vf.append("fade=t=in:st=0:d=0.5")
    elif effects.get("enabled", True) and effects.get("flash_transitions", True):
        # hvit blits inn i klippet der Director valgte cut/speed_ramp
        vf.append("fade=t=in:st=0:d=0.13:color=white")
    if not vf:
        vf = ["null"]  # tom filterkjede er ugyldig i filter_complex
    if letterbox:
        # P6 (justert 2026-07-05): KUN bunn-bånd (160 px), glir inn 0.4 s.
        # Toppbåndet dekket killfeeden — kills gjennom røyk/vegg ble
        # uleselige. Via overlay, IKKE drawbox: i drawbox-uttrykk
        # betyr `t` tykkelse, ikke tid — kun overlay har tidsanimert posisjon.
        # NB: overlay_text ligger på y=h*0.72 (~777) og klarerer båndet (920).
        slide = "min(160,160*t/0.4)"
        chains.append(f"[vc]{','.join(vf)}[vfx]")
        chains.append("color=black:s=1920x160:r=60[lbb]")
        chains.append(f"[vfx][lbb]overlay=x=0:y='1080-{slide}'[vout]")
    else:
        chains.append(f"[vc]{','.join(vf)}[vout]")
    return ";".join(chains), "[vout]", "[ac]"


# P19: instant replay — rewind-lengde, slow-mo-vindu rundt drapet og faktor
REPLAY_REWIND_S = 1.0    # rewind-snuttens lengde i output
REPLAY_PRE_S = 1.8       # kilde-sekunder FØR drapet i replayen
REPLAY_POST_S = 1.2      # kilde-sekunder ETTER drapet
REPLAY_FACTOR = 0.45     # avspillingshastighet i replayen


def pick_replay_clip(plan: EditPlan, candidates_by_id: dict, cfg: dict):
    """P19: hvilket klipp får instant replay av siste drap. Returnerer
    (clip_id, KillMark) eller None.

    Director kan velge eksplisitt via effects_on=["replay"] (maks ett,
    validert). Ellers: klimaks-klippet, hvis det er stort nok (3+ kills
    eller clutch) og ikke har effects_off=["replay"]. Brukes BÅDE av
    render-siden (P17: offer-POV-vinkel) og editing (replay-segmentet) —
    de MÅ være enige om valget.
    """
    fx = cfg.get("effects", {})
    if not (fx.get("enabled", True) and fx.get("instant_replay", False)):
        return None
    decisions = sorted(plan.decisions, key=lambda d: d.order)
    if not decisions:
        return None
    explicit = [d for d in decisions if "replay" in d.effects_on]
    if explicit:
        target = explicit[0]
    else:
        climax = [d for d in decisions if d.is_climax]
        target = climax[0] if climax else decisions[-1]
        if "replay" in target.effects_off:
            return None
        cand = candidates_by_id.get(target.clip_id)
        if cand is None or (
            cand.kill_count < 3 and not (cand.clutch_vs and cand.clutch_vs >= 2)
        ):
            return None  # replay på et svakt klipp er verre enn ingen replay
    kills = getattr(candidates_by_id.get(target.clip_id), "kills", None)
    if not kills:
        return None
    return target.clip_id, kills[-1]


def _build_replay_segment(
    main_seg: Path,
    replay_src: Path,
    kill_offset_s: float,
    kill_t_out: float,
    seg_duration: float,
    effects: dict,
    out_path: Path,
    log_path: Path,
    label: str,
) -> float:
    """P19: bygg rewind + slow-mo-replay som eget segment.

    main_seg: det ferdige klimaks-segmentet (rewind tas fra output-enden,
    tilbake til drapet). replay_src: offer-POV-klippet (_alt) eller samme
    POV som fallback — samme tick-range, så kill_offset_s gjelder begge.
    Returnerer faktisk varighet.
    """
    src_dur = probe_duration(replay_src)
    w0 = max(0.0, kill_offset_s - REPLAY_PRE_S)
    w1 = min(src_dur, kill_offset_s + REPLAY_POST_S)
    rew_from = max(0.0, kill_t_out - 0.4)
    rew_window = max(0.3, seg_duration - rew_from)
    rew_speed = rew_window / REPLAY_REWIND_S

    smooth = (
        effects.get("enabled", True)
        and effects.get("smooth_slowmo", False)
        and probe_fps(replay_src) <= HIGHFPS_THRESHOLD  # P20
    )
    replay_v = (
        f"[1:v]trim=start={w0:.3f}:end={w1:.3f},"
        f"setpts=(PTS-STARTPTS)/{REPLAY_FACTOR:.3f}"
    )
    if smooth:
        replay_v += ",minterpolate=fps=60:mi_mode=mci"
    replay_v += (
        ",fps=60,scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2"
    )
    vf = _effects_filters(effects, None)
    if vf:
        replay_v += "," + ",".join(vf)
    # REPLAY-merke: øverst i midten (killfeed bor øverst til høyre), puls
    replay_v += (
        f",drawtext=fontfile='{FONT_FILE}':text='REPLAY':expansion=none"
        f":fontsize=72:fontcolor=white:borderw=5:bordercolor=black"
        f":x=(w-text_w)/2:y=70:alpha='0.6+0.4*abs(sin(t*3))'"
    )

    chains = [
        # rewind: fra rett før drapet til segmentslutt, reversert i høyt tempo
        f"[0:v]trim=start={rew_from:.3f}:end={seg_duration:.3f},"
        f"setpts=PTS-STARTPTS,reverse,setpts=PTS/{rew_speed:.4f},fps=60,"
        f"scale=1920:1080:force_original_aspect_ratio=decrease,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2[rv]",
        f"aevalsrc=0|0:s=48000:d={REPLAY_REWIND_S:.3f},"
        f"aformat=sample_fmts=fltp:channel_layouts=stereo[ra]",
        f"{replay_v}[pv]",
        f"[1:a]atrim=start={w0:.3f}:end={w1:.3f},asetpts=PTS-STARTPTS,"
        f"{_atempo_chain(REPLAY_FACTOR)}[pa]",
        "[rv][ra][pv][pa]concat=n=2:v=1:a=1[v][a]",
    ]
    run_ffmpeg(
        [
            "-i", str(main_seg),
            "-i", str(replay_src),
            "-filter_complex", ";".join(chains),
            "-map", "[v]", "-map", "[a]",
            *ENCODE_ARGS,
            str(out_path),
        ],
        log_path,
        label,
    )
    return probe_duration(out_path)


def _choose(name: str, default: bool, d_on: set, d_off: set) -> bool:
    """P18: Directors per-klipp-valg. effects_off vinner, effects_on
    overstyrer standardREGELEN — men aldri config-masterbryteren."""
    if name in d_off:
        return False
    if name in d_on:
        return True
    return default


def _timing_plan(decision: EditDecision, cand, duration: float, fx: dict) -> dict:
    """Alt som påvirker segmentets LENGDE, samlet ett sted: speed-parts
    (Director-slowmo + P10-mikro + P3-ramper), P4-freeze og out_duration.

    Brukes av BÅDE build_segments og P21-drop-simuleringen — ble dette
    beregnet to steder hver for seg, ville simulert og faktisk tidslinje
    drive fra hverandre.
    """
    fx_on = fx.get("enabled", True)
    kills = getattr(cand, "kills", None) if cand else None
    k = max(0.0, float(fx.get("intensity", 1.0)))
    d_on = set(decision.effects_on)
    d_off = set(decision.effects_off)

    kill_zoom_active = fx_on and fx.get("kill_zoom", False) and _choose(
        "kill_zoom", True, d_on, d_off
    )

    # P10: velg headshots som får punch (kildetid). Siste kill hoppes
    # over når kill_zoom er på (den har alt sin egen, dypere zoom), og
    # punch-er tettere enn 1.5 s stables ikke.
    hs_marks: list[float] = []
    if (
        fx_on and fx.get("headshot_punch", False) and kills and k > 0
        and _choose("headshot_punch", True, d_on, d_off)
    ):
        last_off = kills[-1].offset_s
        prev = -99.0
        for km in kills:
            if not km.headshot:
                continue
            if kill_zoom_active and abs(km.offset_s - last_off) < 0.01:
                continue
            if km.offset_s - prev < 1.5:
                continue
            hs_marks.append(km.offset_s)
            prev = km.offset_s

    # P10: mikro-slowmo rundt hver punch — hopp over headshots som
    # allerede ligger i et Director-slowmo-segment
    micro: list[tuple[float, float, float]] = []
    if hs_marks and fx.get("headshot_micro_slowmo", True):
        for t in hs_marks:
            in_director_slowmo = any(
                seg.start_s - 0.05 <= t <= seg.end_s + 0.05
                for seg in decision.slowmo_segments
            )
            if in_director_slowmo:
                continue
            s, e = max(0.0, t - 0.10), min(duration, t + 0.20)
            if e - s >= 0.15:
                micro.append((s, e, 0.4))

    parts = _speed_parts(duration, decision, micro)
    if fx_on and fx.get("eased_ramps", True):
        # P3: må skje FØR out_duration — beat-snap og filter skal se
        # nøyaktig samme deler.
        parts = _eased_parts(parts)

    # P4: freeze-frame på siste drap i ace/høy multikill — "punktum".
    # Varigheten telles inn i out_duration så beat-snappen holder.
    freeze = None
    if fx_on and fx.get("freeze_on_finish", False) and kills:
        big_finish = cand.kind == "ace" or (
            cand.kind == "multikill" and cand.kill_count >= 4
        )
        t_last = kills[-1].offset_s
        if (
            _choose("freeze", big_finish, d_on, d_off)
            and 0.5 < t_last < duration - 0.3
        ):
            parts, after_idx = _split_parts_at(parts, t_last)
            freeze = {
                "src_t": t_last,
                "dur": max(0.1, float(fx.get("freeze_seconds", 0.45))),
                "after_idx": after_idx,
            }

    out_duration = sum((end - start) / speed for start, end, speed in parts)
    if freeze:
        out_duration += freeze["dur"]

    return {
        "parts": parts,
        "freeze": freeze,
        "micro": micro,
        "hs_marks": hs_marks,
        "kill_zoom_active": kill_zoom_active,
        "out_duration": out_duration,
    }


# P20: kilder over dette regnes som høy-fps-rendret — slow-mo bruker da
# ekte frames (setpts) og minterpolate hoppes over
HIGHFPS_THRESHOLD = 61.0


def wants_high_fps(decision: EditDecision, cand, fx: dict) -> bool:
    """P20: skal dette klippet rendres i høy fps? Sant hvis noe i klippet
    kommer til å gå i slow-mo: Director-segmenter eller P10-mikro-slowmo.
    (Replay-klippet håndteres i tillegg av render-siden — replayen bremser
    alt-vinkelen uansett.)

    Deles av render-siden (steg 4, FØR klippet finnes) og fasestopp-
    estimatet. Bruker cand.duration_seconds siden råklippet ikke finnes
    ennå — samme _timing_plan-logikk som steg 6 bruker på ekte varighet.
    """
    if decision.slowmo_segments:
        return True
    if cand is None:
        return False
    tp = _timing_plan(decision, cand, float(cand.duration_seconds or 0.0), fx)
    return bool(tp["micro"])


def _climax_clip_id(plan: EditPlan) -> str | None:
    """P18-regelen: Directors is_climax vinner, ellers siste klipp."""
    decisions = sorted(plan.decisions, key=lambda d: d.order)
    if not decisions:
        return None
    explicit = [d.clip_id for d in decisions if d.is_climax]
    return explicit[0] if explicit else decisions[-1].clip_id


def _simulate_climax_start(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    beats: dict,
    cfg: dict,
    timeline_start: float,
    dur_cache: dict,
) -> float | None:
    """P21: forutsi hvor på tidslinja klimaks-klippet starter, med samme
    snapping-regler som build_segments (ren aritmetikk + ffprobe, ingen
    encoding). Returnerer None hvis klimaks-råklippet mangler."""
    editing = cfg.get("editing", {})
    fx = cfg.get("effects", {})
    tolerance = float(editing.get("beat_tolerance_seconds", 0.75))
    target_id = _climax_clip_id(plan)
    if target_id is None:
        return None
    replay_pick = pick_replay_clip(plan, candidates_by_id, cfg)

    pos = timeline_start
    for d in sorted(plan.decisions, key=lambda d: d.order):
        clip = clips_dir / f"{d.clip_id}.mp4"
        if not clip.exists():
            if d.clip_id == target_id:
                return None
            continue  # build_segments hopper også over manglende klipp
        if d.clip_id == target_id:
            return pos
        if d.clip_id not in dur_cache:
            dur_cache[d.clip_id] = probe_duration(clip)
        duration = dur_cache[d.clip_id]
        cand = candidates_by_id.get(d.clip_id)
        tp = _timing_plan(d, cand, duration, fx)
        desired_end = pos + tp["out_duration"]
        snapped = nearest_beat_before(desired_end, beats, tolerance)
        pos = (
            snapped
            if snapped is not None and snapped - pos >= 2.0
            else desired_end
        )
        # sjelden: replay på et klipp FØR klimaks skyver tidslinja
        if replay_pick and replay_pick[0] == d.clip_id:
            src = clips_dir / f"{d.clip_id}_alt.mp4"
            if not src.exists():
                src = clip
            key = f"replay_src::{src.name}"
            if key not in dur_cache:
                dur_cache[key] = probe_duration(src)
            w0 = max(0.0, replay_pick[1].offset_s - REPLAY_PRE_S)
            w1 = min(dur_cache[key], replay_pick[1].offset_s + REPLAY_POST_S)
            pos += REPLAY_REWIND_S + (w1 - w0) / REPLAY_FACTOR
    return None


# P21: intro-kortets lengde er en spak for drop-justering — grensene her
# holder kortet innenfor det som ser bevisst ut (ikke for kort, ikke dødtid)
DROP_INTRO_MIN_S = 1.5
DROP_INTRO_EXTRA_MAX_S = 4.0
DROP_CONVERGED_S = 0.02


def plan_drop_sync(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    beats: dict,
    cfg: dict,
    base_start: float,
) -> dict | None:
    """P21: velg drop-kandidat + justering slik at klimaks-klippet STARTER
    på musikkens drop (action og musikk topper sammen).

    base_start: tidslinjeposisjon der intro-kortet starter (= cold open-
    glimtenes samlede lengde) — introlengden er selve spak 1.
    To spaker, prøvd i den rekkefølgen per drop-kandidat (sterkest først):
      1. intro-justering (±): beholder sporets egen oppbygging fra start
      2. musikk-offset (+): start sporet lenger ut, for drops langt ute
    Returnerer dict med beats (evt. forskjøvet), intro_seconds, music_offset
    og rapport-info — eller None (→ dagens klimaks-til-slutt-oppførsel).
    """
    editing = cfg.get("editing", {})
    if not editing.get("drop_sync", False):
        return None
    drops = beats.get("drops") or []
    if not drops:
        return None
    target_id = _climax_clip_id(plan)
    if target_id is None:
        return None
    base_intro = float(editing.get("intro_seconds", 2.5))
    tolerance = float(editing.get("beat_tolerance_seconds", 0.75))
    lo, hi = DROP_INTRO_MIN_S, base_intro + DROP_INTRO_EXTRA_MAX_S
    dur_cache: dict = {}

    def climax_start(b: dict, intro: float) -> float | None:
        return _simulate_climax_start(
            plan, candidates_by_id, clips_dir, b, cfg,
            base_start + intro, dur_cache,
        )

    c0 = climax_start(beats, base_intro)
    if c0 is None:
        return None

    def result(b, intro, off, method, drop_t, err_s):
        return {
            "beats": b,
            "intro_seconds": round(intro, 3),
            "music_offset": round(off, 3),
            "method": method,
            "climax_clip_id": target_id,
            "drop_track_s": drop_t,
            "drop_timeline_s": round(drop_t - off, 3),
            "predicted_error_ms": round(err_s * 1000),
        }

    for drop in drops:  # sterkest først
        drop_t = float(drop["time_s"])
        r = drop_t - c0

        # Spak 1: intro-justering — droppet ligger nær naturlig klimaks
        if lo - base_intro - tolerance <= r <= hi - base_intro + tolerance:
            intro = min(hi, max(lo, base_intro + r))
            for _ in range(6):
                c = climax_start(beats, intro)
                if c is None:
                    break
                err = drop_t - c
                if abs(err) <= DROP_CONVERGED_S:
                    break
                new_intro = min(hi, max(lo, intro + err))
                if abs(new_intro - intro) < 0.005:
                    break
                intro = new_intro
            c = climax_start(beats, intro)
            if c is not None and abs(c - drop_t) <= tolerance:
                return result(beats, intro, 0.0, "intro", drop_t, c - drop_t)

        # Spak 2: musikk-offset — droppet ligger langt UT i sporet
        if r > 0:
            off = r
            for _ in range(6):
                c = climax_start(shift_beats(beats, off), base_intro)
                if c is None or off <= 0:
                    break
                err = (drop_t - off) - c
                if abs(err) <= DROP_CONVERGED_S:
                    break
                off += err
            if off > 0:
                shifted = shift_beats(beats, off)
                c = climax_start(shifted, base_intro)
                if c is not None and abs(c - (drop_t - off)) <= tolerance:
                    return result(
                        shifted, base_intro, off, "offset",
                        drop_t, c - (drop_t - off),
                    )
    return None


def build_segments(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    segments_dir: Path,
    beats: dict,
    cfg: dict,
    log_path: Path,
    context: dict | None = None,
    timeline_start: float | None = None,
) -> tuple[list[Path], list[dict], list[str]]:
    """Returnerer (segmentfiler i rekkefølge, cut-logg, flagg).

    context: valgfri demo-kontekst utover kandidatene, i dag kun
    {"map": "de_inferno"} (brukes av P7-lowerthird)."""
    editing = cfg.get("editing", {})
    fx = cfg.get("effects", {})
    tolerance = float(editing.get("beat_tolerance_seconds", 0.75))
    # P21/P14: kalleren kan angi hvor på tidslinja segmentene faktisk starter
    # (cold open-glimt + evt. drop-justert intro); default som før.
    timeline_pos = (
        float(editing.get("intro_seconds", 2.5))
        if timeline_start is None
        else float(timeline_start)
    )

    segments_dir.mkdir(parents=True, exist_ok=True)
    segment_files: list[Path] = []
    cut_log: list[dict] = []
    flags: list[str] = []
    climax_order = max((d.order for d in plan.decisions), default=0)
    # P18: har Director pekt ut klimaks eksplisitt, vinner det over
    # "siste klipp"-regelen
    climax_ids = {d.clip_id for d in plan.decisions if d.is_climax}

    # P5: manglende LUT-fil skal gi tydelig flagg, ikke ffmpeg-krasj.
    # Relativ sti ankres i prosjektmappa (jf. sfx_dir), ikke i CWD.
    lut = str(fx.get("lut", "") or "")
    if lut:
        lut_path = Path(lut)
        if not lut_path.is_absolute():
            lut_path = Path(__file__).resolve().parent.parent / lut_path
        if lut_path.is_file():
            fx = {**fx, "lut": str(lut_path)}
        else:
            flags.append(f"LUT file not found: {lut} — grading runs without a LUT.")
            fx = {**fx, "lut": ""}

    # P19: instant replay — samme utvalg som render-siden gjorde (P17)
    replay_pick = pick_replay_clip(plan, candidates_by_id, cfg)

    for decision in sorted(plan.decisions, key=lambda d: d.order):
        clip_path = clips_dir / f"{decision.clip_id}.mp4"
        if not clip_path.exists():
            flags.append(
                f"Raw clip missing for {decision.clip_id} — skipped "
                f"(a render error on one clip shouldn't stop the film)."
            )
            continue

        duration = probe_duration(clip_path)
        fx_on = fx.get("enabled", True)
        cand = candidates_by_id.get(decision.clip_id)
        kills = getattr(cand, "kills", None) if cand else None
        k = max(0.0, float(fx.get("intensity", 1.0)))

        # P18: Directors per-klipp-valg (se _choose). Tidslogikken deles
        # med P21-simuleringen via _timing_plan.
        d_on = set(decision.effects_on)
        d_off = set(decision.effects_off)

        def choose(name: str, default: bool) -> bool:
            return _choose(name, default, d_on, d_off)

        tp = _timing_plan(decision, cand, duration, fx)
        parts, freeze, micro = tp["parts"], tp["freeze"], tp["micro"]
        hs_marks, kill_zoom_active = tp["hs_marks"], tp["kill_zoom_active"]
        out_duration = tp["out_duration"]

        def _out_time(t_src: float) -> float:
            """Kilde- → output-tid, inkl. skiftet fra en innsatt freeze."""
            t = source_to_output_time(t_src, parts)
            if freeze and t_src > freeze["src_t"] + 1e-6:
                t += freeze["dur"]
            return t

        # Beat-justering: snapp segmentSLUTT til nærmeste beat (kun trimming)
        desired_end = timeline_pos + out_duration
        snapped = nearest_beat_before(desired_end, beats, tolerance)
        target_duration = out_duration
        trimmed_ms = None
        if snapped is not None and snapped - timeline_pos >= 2.0:
            target_duration = snapped - timeline_pos
            trimmed_ms = round((desired_end - snapped) * 1000)
        else:
            flags.append(
                f"{decision.clip_id}: no beat within ±{tolerance}s — "
                f"beat sync skipped for this cut."
            )

        # Beat-kontekst for effekter: periode + fase (første beat etter
        # segmentets plassering på tidslinja, relativt til segmentstart)
        period = 60.0 / beats["bpm"] if beats.get("bpm") else None
        beat_ctx = None
        if period:
            next_beat = next(
                (t for t in beats["beat_times"] if t >= timeline_pos), None
            )
            phase = (next_beat - timeline_pos) % period if next_beat else 0.0
            beat_ctx = {"period": period, "phase": phase}

        # P6: letterbox på klimaks — slow-mo-segmenter og clutcher
        letterbox = (
            fx_on
            and fx.get("letterbox_on_slowmo", False)
            and choose(
                "letterbox",
                bool(decision.slowmo_segments)
                or getattr(cand, "kind", None) == "clutch",
            )
        )

        # Kill-punch: dyp crosshair-zoom på klippets SISTE kill; dutch tilt
        # kun på filmens klimaks-kill (høyeste order). Tider mappes fra
        # kilde- til output-tid, og drapet må ligge trygt innenfor det
        # beat-trimmede segmentet.
        kill_fx = None
        if kill_zoom_active and kills:
            t_out = _out_time(kills[-1].offset_s)
            # med freeze må både frysen og snap-out få plass før segmentslutt
            room = (freeze["dur"] if freeze else 0.0) + 0.7
            if 0.8 < t_out < target_duration - room:
                is_climax = (
                    decision.clip_id in climax_ids
                    if climax_ids
                    else decision.order == climax_order
                )
                tilt_deg = 0.0
                if fx.get("kill_tilt", False) and is_climax:
                    tilt_deg = float(fx.get("kill_tilt_degrees", 22.0))
                kill_fx = {"t": t_out, "tilt_deg": tilt_deg}
                if freeze:
                    # hold zoomen gjennom frysen, snap ut når bildet våkner
                    kill_fx["hold_until"] = t_out + freeze["dur"] + 0.10

        # P10: punch-tider i output-tid, trygt innenfor trimmet segment
        hs_punches: list[dict] = []
        strength = max(0.0, float(fx.get("headshot_punch_strength", 1.0)))
        dz = min(0.22, 0.15 * strength * k)
        if hs_marks and dz > 0.01:
            for t in hs_marks:
                t_out = _out_time(t)
                if 0.5 < t_out < target_duration - 0.25:
                    hs_punches.append({"t": t_out, "dz": dz})

        # P11: kill-teller på multikill/ace (>= 3 drap). Siste tikk viser
        # "4K"/"ACE" — lander den på en P4-freeze blir det stående punktum.
        counter = None
        if (
            fx_on and fx.get("kill_counter", False) and kills
            and len(kills) >= 2
            and choose("kill_counter", len(kills) >= 3)
        ):
            counter = []
            for i, km in enumerate(kills):
                t_out = _out_time(km.offset_s)
                if t_out >= target_duration - 0.05:
                    break
                label = str(i + 1)
                if i == len(kills) - 1 and len(kills) >= 4:
                    label = "ACE" if len(kills) >= 5 else "4K"
                counter.append((t_out, label))
            counter = counter or None

        # P7: kontekstlinje fra Highlight-felt (aldri fra Director) —
        # manglende felt hoppes over, aldri tom "None"
        lowerthird = None
        if fx_on and fx.get("lowerthird", False) and cand:
            ctx_parts = []
            map_name = str((context or {}).get("map", "") or "")
            if map_name:
                ctx_parts.append(map_name)
            if getattr(cand, "round", 0):
                ctx_parts.append(f"round {cand.round}")
            clutch_vs = getattr(cand, "clutch_vs", None)
            if clutch_vs:
                ctx_parts.append(f"1v{clutch_vs}")
            if ctx_parts:
                lowerthird = "  ·  ".join(ctx_parts)

        # P12 + P15: "store drap" = punch-utvalget (headshots + siste kill).
        # Samme tider brukes til RGB-split i bildet og impact-SFX i miksen.
        impact_times = sorted(
            [p["t"] for p in hs_punches]
            + ([kill_fx["t"]] if kill_fx else [])
        )
        impacts = (
            impact_times
            if fx_on and fx.get("impact_split", False) and impact_times
            and choose("impact_split", True)
            else None
        )

        filter_complex, vout, aout = _build_filter(
            parts, decision, fx, beat_ctx, letterbox, kill_fx,
            freeze, hs_punches, [(s, e) for s, e, _ in micro],
            counter, lowerthird, impacts,
            src_fps=probe_fps(clip_path),  # P20: høy-fps-kilde → ekte slow-mo
        )
        out_path = segments_dir / f"{decision.order:02d}_{decision.clip_id}.mp4"
        run_ffmpeg(
            [
                "-i", str(clip_path),
                "-filter_complex", filter_complex,
                "-map", vout, "-map", aout,
                "-t", f"{target_duration:.3f}",
                *ENCODE_ARGS,
                str(out_path),
            ],
            log_path,
            f"step 6: segment {decision.order:02d} ({decision.clip_id})",
        )
        actual = probe_duration(out_path)
        # Faktisk synk-feil: hvor langt kuttet lander fra beatet etter
        # encoding (frame-granularitet ~17 ms ved 60 fps). Kravet er ±100 ms.
        beat_offset_ms = (
            round((timeline_pos + actual - snapped) * 1000)
            if snapped is not None and trimmed_ms is not None
            else None
        )
        cut_log.append(
            {
                "clip_id": decision.clip_id,
                "order": decision.order,
                "source_duration_s": round(duration, 3),
                "segment_duration_s": round(actual, 3),
                "trimmed_to_hit_beat_ms": trimmed_ms,
                "beat_offset_ms": beat_offset_ms,
                "timeline_end_s": round(timeline_pos + actual, 3),
                # P15: impact-SFX mikses på disse tidene (relative til
                # segmentstart) i assembly
                "sfx_impact_times_s": [round(t, 3) for t in impact_times],
            }
        )
        timeline_pos += actual
        segment_files.append(out_path)

        # P19: rewind + slow-mo-replay rett etter klimaks-segmentet
        if replay_pick and replay_pick[0] == decision.clip_id:
            replay_kill = replay_pick[1]
            alt_path = clips_dir / f"{decision.clip_id}_alt.mp4"
            replay_src = alt_path
            if not alt_path.exists():
                flags.append(
                    f"{decision.clip_id}: victim-POV clip (_alt) missing — "
                    f"replay is built from the same POV instead."
                )
                replay_src = clip_path
            replay_out = segments_dir / (
                f"{decision.order:02d}b_{decision.clip_id}_replay.mp4"
            )
            replay_dur = _build_replay_segment(
                out_path, replay_src,
                replay_kill.offset_s, _out_time(replay_kill.offset_s),
                actual, fx, replay_out, log_path,
                f"step 6: replay ({decision.clip_id})",
            )
            # impact-SFX når drapet lander i replayen (etter rewinden)
            kill_in_replay = REPLAY_REWIND_S + (
                min(replay_kill.offset_s, REPLAY_PRE_S) / REPLAY_FACTOR
            )
            cut_log.append(
                {
                    "clip_id": f"{decision.clip_id}_replay",
                    "order": f"{decision.order}b",
                    "segment_duration_s": round(replay_dur, 3),
                    "timeline_end_s": round(timeline_pos + replay_dur, 3),
                    "sfx_impact_times_s": [round(kill_in_replay, 3)],
                }
            )
            timeline_pos += replay_dur
            segment_files.append(replay_out)

    return segment_files, cut_log, flags
