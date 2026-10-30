"""Steg 7: Assembly — intro/outro-kort, sammensying, musikkmiks, eksport.

Game-lyd duckes under musikken; musikken fades ut mot slutten.
Output: runs/<id>/final.mp4 + report.md.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lib.edit_engine import ENCODE_ARGS
from lib.ffmpeg_util import (
    FONT_FILE,
    drawtext_escape,
    probe_duration,
    run_ffmpeg,
)
from lib.models import EditPlan


def _text_card(
    text: str, seconds: float, out_path: Path, log_path: Path, label: str
) -> None:
    escaped = drawtext_escape(text or " ")
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:r=60:d={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(seconds),
            "-vf",
            (
                f"drawtext=fontfile='{FONT_FILE}':text='{escaped}':expansion=none"
                f":fontsize=72:fontcolor=white:borderw=3:bordercolor=black"
                f":x=(w-text_w)/2:y=(h-text_h)/2,fade=t=in:st=0:d=0.4,"
                f"fade=t=out:st={seconds - 0.4}:d=0.4"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "60",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_path),
        ],
        log_path,
        f"steg 7: {label}",
    )


def _ensure_sfx(sfx_dir: Path, log_path: Path) -> dict[str, Path]:
    """P15: SFX-filene syntetiseres med ffmpeg hvis de mangler — rettighets-
    frie by construction, ingen binærfiler i repoet. Legg egne .wav-er i
    sfx_dir for å overstyre (de genereres kun når fila ikke finnes)."""
    sfx_dir.mkdir(parents=True, exist_ok=True)
    whoosh = sfx_dir / "whoosh.wav"
    impact = sfx_dir / "impact.wav"
    if not whoosh.exists():
        # pink-noise-riser (0.6 s) som stiger inn mot kuttet
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:r=48000:a=0.7",
                "-af",
                "highpass=f=300,lowpass=f=5000,"
                "afade=t=in:d=0.45:curve=exp,afade=t=out:st=0.45:d=0.15,"
                # båndpassfiltrene spiser mye energi — løft, med limiter som
                # tak så peak aldri klipper (jf. impact-skurringen)
                "volume=3.0,alimiter=limit=0.95",
                "-ac", "2", str(whoosh),
            ],
            log_path,
            "steg 7: syntetiserer whoosh.wav (P15)",
        )
    if not impact.exists():
        # 52 Hz-dunk med dempet klikk-transient. NB: summen av gain-ene må
        # holde peak < 1.0 — volume-boost ETTER amix klippet 16-bit-waven
        # til firkantbølge (høyfrekvent skurr på hvert drap, 2026-07-05).
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", "sine=f=52:d=0.5:r=48000",
                "-f", "lavfi", "-i", "anoisesrc=d=0.03:c=white:r=48000:a=0.4",
                "-filter_complex",
                # sine-kilden er lavmælt (~-18 dB) — løft bassen kraftig,
                # klikket forsiktig, og la limiteren garantere peak < 1.0
                "[0:a]afade=t=out:st=0.02:d=0.46,volume=6.0[bass];"
                "[1:a]lowpass=f=2500,volume=0.6[click];"
                "[bass][click]amix=inputs=2:duration=first:normalize=0,"
                "alimiter=limit=0.95[sfx]",
                "-map", "[sfx]", "-ac", "2", str(impact),
            ],
            log_path,
            "steg 7: syntetiserer impact.wav (P15)",
        )
    return {"whoosh": whoosh, "impact": impact}


def player_stats(
    events: dict | None, pov_steamid: str, candidates_by_id: dict | None = None
) -> dict | None:
    """P13: aggreger POV-stats fra events. Returnerer None hvis grunnlaget
    mangler (→ fall tilbake til vanlig tekstkort). HS% utelates ved < 5 kills
    (meningsløse prosenter, jf. spec-en)."""
    if not events or not pov_steamid:
        return None
    all_kills = events.get("kills", [])
    kills = [
        k for k in all_kills
        if k.get("attacker_steamid") == pov_steamid
        and k.get("victim_steamid") != pov_steamid
    ]
    if not kills:
        return None
    deaths = sum(1 for k in all_kills if k.get("victim_steamid") == pov_steamid)
    hs = sum(1 for k in kills if k.get("headshot"))
    best_clutch = 0
    for c in (candidates_by_id or {}).values():
        vs = getattr(c, "clutch_vs", None) or 0
        best_clutch = max(best_clutch, vs)
    return {
        "kills": len(kills),
        "deaths": deaths,
        "kd": len(kills) / max(1, deaths),
        "hs_pct": round(100 * hs / len(kills)) if len(kills) >= 5 else None,
        "map": str(events.get("map", "") or ""),
        "best_clutch": best_clutch if best_clutch >= 2 else None,
    }


def _stats_card(
    title: str,
    player_name: str,
    stats: dict,
    seconds: float,
    out_path: Path,
    log_path: Path,
) -> None:
    """P13: outro med stats i stedet for bare tekst. Samme fade som _text_card."""
    line1 = " — ".join(x for x in (player_name, stats["map"]) if x)
    line2 = f"{stats['kills']} kills  ·  K/D {stats['kd']:.2f}"
    if stats["hs_pct"] is not None:
        line2 += f"  ·  {stats['hs_pct']}% HS"
    rows = [
        (title or "GG", 96, 0.30),
        (line1, 44, 0.48),
        (line2, 40, 0.58),
    ]
    if stats["best_clutch"]:
        rows.append((f"Beste clutch: 1v{stats['best_clutch']}", 40, 0.67))
    draw = ",".join(
        f"drawtext=fontfile='{FONT_FILE}':text='{drawtext_escape(txt)}'"
        f":expansion=none"
        f":fontsize={size}:fontcolor=white:borderw=3:bordercolor=black"
        f":x=(w-text_w)/2:y=h*{ypos:.2f}"
        for txt, size, ypos in rows
        if txt.strip()
    )
    run_ffmpeg(
        [
            "-f", "lavfi", "-i", f"color=c=black:s=1920x1080:r=60:d={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(seconds),
            "-vf",
            f"{draw},fade=t=in:st=0:d=0.4,fade=t=out:st={seconds - 0.4}:d=0.4",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "60",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_path),
        ],
        log_path,
        "steg 7: stats-outro",
    )


def build_intro_hook(
    plan: EditPlan,
    candidates_by_id: dict | None,
    clips_dir: Path | None,
    work: Path,
    cfg: dict,
    log_path: Path,
) -> list[Path]:
    """P14/"cold open": inntil 3 mikro-glimt (0.25 s) av toppdrapene, klippet
    rett fra råklippene rundt KillMark-offsets, foran intro-kortet.
    Rekkefølge: stigende hype — beste glimt sist, rett før tittelen.

    Kalles av main.py FØR build_segments: glimtene ligger foran introen på
    tidslinja, så beat-synken i steg 6 må vite samlet glimt-lengde (uten
    det var all beat-sync ~0.75 s off når intro_hook var på — bug funnet
    under P21-arbeidet 2026-07-07)."""
    fx = cfg.get("effects", {})
    if not (fx.get("enabled", True) and fx.get("intro_hook", False)):
        return []
    if not candidates_by_id or clips_dir is None:
        return []

    picks: list[tuple[float, Path, float]] = []
    for d in plan.decisions:
        cand = candidates_by_id.get(d.clip_id)
        kills = getattr(cand, "kills", None) if cand else None
        clip = clips_dir / f"{d.clip_id}.mp4"
        if not kills or not clip.exists():
            continue
        k = kills[-1]
        score = cand.hype_score + (0.5 if k.headshot else 0.0)
        picks.append((score, clip, k.offset_s))
    picks = sorted(sorted(picks, key=lambda p: -p[0])[:3], key=lambda p: p[0])

    glimt: list[Path] = []
    for i, (_, clip, offset) in enumerate(picks):
        out = work / f"00_hook_{i}.mp4"
        run_ffmpeg(
            [
                "-ss", f"{max(0.0, offset - 0.15):.3f}", "-i", str(clip),
                "-t", "0.25",
                "-vf",
                (
                    "scale=1920:1080:force_original_aspect_ratio=decrease,"
                    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                    "fade=t=in:st=0:d=0.05:color=white"
                ),
                "-af", "asetpts=PTS-STARTPTS",
                *ENCODE_ARGS,
                str(out),
            ],
            log_path,
            f"steg 7: cold open-glimt {i + 1}",
        )
        glimt.append(out)
    return glimt


def assemble(
    plan: EditPlan,
    segment_files: list[Path],
    music_path: Path,
    run_dir: Path,
    cfg: dict,
    log_path: Path,
    candidates_by_id: dict | None = None,
    clips_dir: Path | None = None,
    events: dict | None = None,
    cut_log: list[dict] | None = None,
    hook_files: list[Path] | None = None,
    intro_seconds: float | None = None,
    music_offset: float = 0.0,
) -> Path:
    """hook_files: ferdigbygde cold open-glimt (fra build_intro_hook) —
    None = bygg dem her (bakoverkompatibelt). intro_seconds overstyrer
    config (P21: intro-justering). music_offset (P21): start musikken så
    mange sekunder UT i sporet, med kort fade-inn."""
    editing = cfg.get("editing", {})
    intro_s = (
        float(editing.get("intro_seconds", 2.5))
        if intro_seconds is None
        else float(intro_seconds)
    )
    outro_s = float(editing.get("outro_seconds", 2.5))
    game_vol = float(editing.get("game_audio_volume", 0.25))

    work = run_dir / "04-segments"
    intro = work / "00_intro.mp4"
    outro = work / "99_outro.mp4"
    _text_card(plan.intro_text, intro_s, intro, log_path, "intro-kort")

    # P13: stats-outro når [editing].stats_outro og grunnlaget finnes —
    # ellers vanlig tekstkort (aldri halvtomt stats-kort)
    stats = None
    pov_name = ""
    if editing.get("stats_outro", False) and candidates_by_id:
        any_cand = next(iter(candidates_by_id.values()), None)
        if any_cand is not None:
            pov_name = getattr(any_cand, "player_name", "")
            stats = player_stats(
                events, getattr(any_cand, "player_steamid", ""), candidates_by_id
            )
    if stats:
        # litt lenger kort — tre-fire linjer trenger lesetid
        _stats_card(plan.outro_text, pov_name, stats,
                    max(outro_s, 3.5), outro, log_path)
    else:
        _text_card(plan.outro_text, outro_s, outro, log_path, "outro-kort")

    hook = (
        hook_files
        if hook_files is not None
        else build_intro_hook(plan, candidates_by_id, clips_dir, work, cfg, log_path)
    )
    ordered = [*hook, intro, *segment_files, outro]
    concat_list = work / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in ordered),
        encoding="utf-8",
    )
    timeline = work / "timeline.mp4"
    run_ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy",
         str(timeline)],
        log_path,
        "steg 7: concat av segmenter",
    )

    total = probe_duration(timeline)
    fade_start = max(0.0, total - 3.0)
    final = run_dir / "final.mp4"

    # P15: SFX-lag — whoosh inn mot hvert klippbytte, impact på store drap
    # (samme tider som P12-splitten, fra cut_log). Under musikken i nivå.
    sfx_inputs: list[str] = []
    sfx_chains: list[str] = []
    sfx_labels: list[str] = []
    if editing.get("sfx", False):
        sfx_dir = Path(editing.get("sfx_dir", "assets/sfx"))
        if not sfx_dir.is_absolute():
            # relativ sti ankres i prosjektmappa, ikke i CWD
            sfx_dir = Path(__file__).resolve().parent.parent / sfx_dir
        sfx = _ensure_sfx(sfx_dir, log_path)
        vol = float(editing.get("sfx_volume", 0.35))
        seg_starts: list[float] = []
        pos = 0.0
        seg_set = {p.resolve() for p in segment_files}
        for p in ordered:
            if p.resolve() in seg_set:
                seg_starts.append(pos)
            pos += probe_duration(p)
        whoosh_times = [max(0.0, s - 0.30) for s in seg_starts]
        impact_events = [
            s + t
            for s, entry in zip(seg_starts, cut_log or [])
            for t in entry.get("sfx_impact_times_s", [])
        ]
        for name, times, gain in (
            ("whoosh", whoosh_times, vol),
            ("impact", impact_events, vol * 1.3),
        ):
            if not times:
                continue
            idx = 2 + len(sfx_inputs)
            sfx_inputs.append(str(sfx[name]))
            outs = "".join(f"[{name}{i}]" for i in range(len(times)))
            sfx_chains.append(
                f"[{idx}:a]volume={gain:.2f},asplit={len(times)}{outs}"
            )
            for i, t in enumerate(times):
                sfx_chains.append(
                    f"[{name}{i}]adelay=delays={int(t * 1000)}:all=1[{name}d{i}]"
                )
                sfx_labels.append(f"[{name}d{i}]")

    n_mix = 2 + len(sfx_labels)
    sfx_in = "".join(sfx_labels)
    # P21: music_offset > 0 → sporet starter midt i (droppet skal treffe
    # klimaks); kort fade-inn så innhoppet ikke smeller
    off = max(0.0, float(music_offset))
    music_chain = (
        f"[1:a]atrim={off:.3f}:{off + total:.3f},asetpts=PTS-STARTPTS,"
        + ("afade=t=in:st=0:d=0.8," if off > 0 else "")
        + f"afade=t=out:st={fade_start:.3f}:d=3"
    )
    if editing.get("sidechain_duck", False):
        # P16: game-lyd dukkes dynamisk av musikkens faktiske nivå
        # (sidechaincompress) i stedet for fast volum — når musikken fader
        # ut mot slutten, slipper game-lyden opp igjen av seg selv.
        thr = float(editing.get("sidechain_threshold", 0.05))
        ratio = float(editing.get("sidechain_ratio", 8.0))
        release = float(editing.get("sidechain_release_ms", 300))
        mix = (
            f"[0:a]volume={game_vol}[game];"
            f"{music_chain},asplit=2[m1][m2];"
            f"[game][m2]sidechaincompress=threshold={thr}:ratio={ratio}"
            f":attack=20:release={release}[gduck];"
            f"[gduck][m1]{sfx_in}amix=inputs={n_mix}"
            f":duration=first:normalize=0[amiks]"
        )
    else:
        mix = (
            f"[0:a]volume={game_vol}[game];"
            f"{music_chain}[musikk];"
            f"[game][musikk]{sfx_in}amix=inputs={n_mix}"
            f":duration=first:normalize=0[amiks]"
        )
    if sfx_chains:
        mix = ";".join(sfx_chains) + ";" + mix
    run_ffmpeg(
        [
            "-i", str(timeline),
            "-i", str(music_path),
            *[a for p in sfx_inputs for a in ("-i", p)],
            "-filter_complex", mix,
            "-map", "0:v", "-map", "[amiks]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
            str(final),
        ],
        log_path,
        "steg 7: musikkmiks + eksport",
    )
    return final


def write_report(
    plan: EditPlan,
    candidates_by_id: dict,
    cut_log: list[dict],
    flags: list[str],
    beats: dict,
    final_path: Path,
    run_dir: Path,
    events: dict | None = None,
) -> Path:
    final_duration = probe_duration(final_path)
    lines = [
        f"# Fragsmith-rapport — {Path(plan.demo_path).name}",
        "",
        f"**Generert:** {datetime.now():%Y-%m-%d %H:%M}",
        f"**Film:** {final_path.name} ({final_duration:.1f} s)",
        f"**Musikk:** {plan.music_track} ({beats.get('bpm', '?')} BPM)",
    ]
    # P21: drop-sync-avgjørelsen (lagt i beats-dicten av cmd_finish)
    ds = beats.get("drop_sync")
    if ds:
        method = (
            "intro-justering"
            if ds["method"] == "intro"
            else f"musikk-offset {ds['music_offset']:.1f} s"
        )
        err = ds.get("actual_error_ms", ds.get("predicted_error_ms", 0))
        lines.append(
            f"**Drop-sync (P21):** klimaks ({ds['climax_clip_id']}) lagt på "
            f"droppet ved {ds['drop_track_s']:.1f} s i sporet via {method} — "
            f"avvik {err:+d} ms"
        )
    # P13: samme tall som stats-outroen viser, så de kan kontrolleres her
    any_cand = next(iter(candidates_by_id.values()), None)
    stats = player_stats(
        events, getattr(any_cand, "player_steamid", ""), candidates_by_id
    ) if any_cand else None
    if stats:
        line = (
            f"**POV-stats:** {stats['kills']} kills / {stats['deaths']} deaths "
            f"(K/D {stats['kd']:.2f})"
        )
        if stats["hs_pct"] is not None:
            line += f", {stats['hs_pct']}% HS"
        if stats["best_clutch"]:
            line += f", beste clutch 1v{stats['best_clutch']}"
        lines.append(line)
    lines += [
        "",
        "## Klipp som ble med",
        "",
        "| # | klipp | hype | lengde | overlay | beat-synk (mål ±100 ms) |",
        "|---|-------|------|--------|---------|--------------------------|",
    ]
    by_order = {c["order"]: c for c in cut_log}
    for d in sorted(plan.decisions, key=lambda d: d.order):
        cand = candidates_by_id.get(d.clip_id)
        cut = by_order.get(d.order, {})
        offset = cut.get("beat_offset_ms")
        lines.append(
            f"| {d.order} | {d.clip_id} | "
            f"{cand.hype_score if cand else '?'} | "
            f"{cut.get('segment_duration_s', '?')} s | "
            f"{d.overlay_text or '—'} | "
            f"{f'{offset:+d} ms' if offset is not None else 'fraveket'} |"
        )
    lines += ["", "## Directors begrunnelser", ""]
    for d in sorted(plan.decisions, key=lambda d: d.order):
        # P18: vis effektvalgene Director gjorde utover standardreglene
        choices = []
        if d.is_climax:
            choices.append("klimaks")
        choices += [f"+{e}" for e in d.effects_on]
        choices += [f"−{e}" for e in d.effects_off]
        suffix = f" *(Director-valg: {', '.join(choices)})*" if choices else ""
        lines.append(f"- **{d.clip_id}:** {d.reasoning}{suffix}")
    if plan.dropped_clip_ids:
        lines += ["", "## Droppede kandidater", ""]
        for cid in plan.dropped_clip_ids:
            cand = candidates_by_id.get(cid)
            score = f" (hype {cand.hype_score})" if cand else ""
            lines.append(f"- {cid}{score}")
    if flags:
        lines += ["", "## Flagg", ""]
        lines += [f"- {f}" for f in flags]
    lines += [
        "",
        "## Cut-logg (beat-sync)",
        "",
        "```json",
        json.dumps(cut_log, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    report = run_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
