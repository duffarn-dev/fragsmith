"""Step 7: assembly. Intro/outro cards, stitching, music mix, export.

Game audio is ducked under the music; the music fades out toward the end.
Output: runs/<id>/final.mp4 + report.md.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lib.branding import branding_files, card_background_filter, logo_overlay
from lib.edit_engine import ENCODE_ARGS, LANDSCAPE, Frame
from lib.ffmpeg_util import (
    FONT_FILE,
    drawtext_escape,
    probe_duration,
    run_ffmpeg,
)
from lib.models import EditPlan
from lib.version import __version__


def _card_source(
    seconds: float, background: Path | None, frame: Frame = LANDSCAPE
) -> tuple[list[str], str]:
    """The video input of a card and the start of its filter chain: black,
    or your [branding] image filling the frame."""
    if background is None:
        return ["-f", "lavfi", "-i", f"color=c=black:s={frame.size}:r=60:d={seconds}"], ""
    return (
        ["-loop", "1", "-framerate", "60", "-t", f"{seconds}", "-i", str(background)],
        card_background_filter(frame.w, frame.h) + ",",
    )


def _text_card(
    text: str, seconds: float, out_path: Path, log_path: Path, label: str,
    background: Path | None = None, frame: Frame = LANDSCAPE,
) -> None:
    escaped = drawtext_escape(text or " ")
    video_in, prefix = _card_source(seconds, background, frame)
    run_ffmpeg(
        [
            *video_in,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(seconds),
            "-vf",
            (
                f"{prefix}drawtext=fontfile='{FONT_FILE}':text='{escaped}':expansion=none"
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
        f"step 7: {label}",
    )


def _ensure_sfx(sfx_dir: Path, log_path: Path) -> dict[str, Path]:
    """The SFX files are synthesized with ffmpeg if they're missing, so
    they're royalty-free by construction. Put your own .wav files in
    sfx_dir to override them (they're only generated when the file is
    missing)."""
    sfx_dir.mkdir(parents=True, exist_ok=True)
    whoosh = sfx_dir / "whoosh.wav"
    impact = sfx_dir / "impact.wav"
    if not whoosh.exists():
        # pink-noise riser (0.6 s) that builds into the cut
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", "anoisesrc=d=0.6:c=pink:r=48000:a=0.7",
                "-af",
                "highpass=f=300,lowpass=f=5000,"
                "afade=t=in:d=0.45:curve=exp,afade=t=out:st=0.45:d=0.15,"
                # the band-pass filters eat a lot of energy: boost it, with a
                # limiter as ceiling so the peak never clips (see impact below)
                "volume=3.0,alimiter=limit=0.95",
                "-ac", "2", str(whoosh),
            ],
            log_path,
            "step 7: synthesizing whoosh.wav",
        )
    if not impact.exists():
        # 52 Hz thump with a soft click transient. Note: the sum of the gains
        # must keep the peak < 1.0. A volume boost AFTER amix clipped the
        # 16-bit wav into a square wave (high-pitched crackle on every kill,
        # 2026-07-05).
        run_ffmpeg(
            [
                "-f", "lavfi", "-i", "sine=f=52:d=0.5:r=48000",
                "-f", "lavfi", "-i", "anoisesrc=d=0.03:c=white:r=48000:a=0.4",
                "-filter_complex",
                # the sine source is quiet (~-18 dB): boost the bass hard,
                # the click gently, and let the limiter guarantee peak < 1.0
                "[0:a]afade=t=out:st=0.02:d=0.46,volume=6.0[bass];"
                "[1:a]lowpass=f=2500,volume=0.6[click];"
                "[bass][click]amix=inputs=2:duration=first:normalize=0,"
                "alimiter=limit=0.95[sfx]",
                "-map", "[sfx]", "-ac", "2", str(impact),
            ],
            log_path,
            "step 7: synthesizing impact.wav",
        )
    return {"whoosh": whoosh, "impact": impact}


def player_stats(
    events: dict | None, pov_steamid: str, candidates_by_id: dict | None = None
) -> dict | None:
    """Aggregate POV stats from events. Returns None if the data is missing
    (→ fall back to a plain text card). HS% is left out below 5 kills
    (the percentage would be meaningless)."""
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
    background: Path | None = None,
    frame: Frame = LANDSCAPE,
) -> None:
    """Outro with stats instead of just text. Same fade as _text_card."""
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
        rows.append((f"Best clutch: 1v{stats['best_clutch']}", 40, 0.67))
    draw = ",".join(
        f"drawtext=fontfile='{FONT_FILE}':text='{drawtext_escape(txt)}'"
        f":expansion=none"
        f":fontsize={size}:fontcolor=white:borderw=3:bordercolor=black"
        f":x=(w-text_w)/2:y=h*{ypos:.2f}"
        for txt, size, ypos in rows
        if txt.strip()
    )
    video_in, prefix = _card_source(seconds, background, frame)
    run_ffmpeg(
        [
            *video_in,
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(seconds),
            "-vf",
            f"{prefix}{draw},fade=t=in:st=0:d=0.4,fade=t=out:st={seconds - 0.4}:d=0.4",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "60",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            str(out_path),
        ],
        log_path,
        "step 7: stats outro",
    )


def build_intro_hook(
    plan: EditPlan,
    candidates_by_id: dict | None,
    clips_dir: Path | None,
    work: Path,
    cfg: dict,
    log_path: Path,
    frame: Frame = LANDSCAPE,
) -> list[Path]:
    """Cold open: up to 3 micro glimpses (0.25 s) of the top kills, cut
    straight from the raw clips around the KillMark offsets, placed before
    the intro card. Order: rising hype, best glimpse last, right before
    the title.

    Called by main.py BEFORE build_segments: the glimpses sit before the
    intro on the timeline, so the beat sync in step 6 needs to know their
    total length (without it, all beat sync was ~0.75 s off when intro_hook
    was on; bug found 2026-07-07)."""
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

    glimpses: list[Path] = []
    for i, (_, clip, offset) in enumerate(picks):
        out = work / f"00_hook_{i}.mp4"
        run_ffmpeg(
            [
                "-ss", f"{max(0.0, offset - 0.15):.3f}", "-i", str(clip),
                "-t", "0.25",
                "-vf", f"{frame.normalize()},fade=t=in:st=0:d=0.05:color=white",
                "-af", "asetpts=PTS-STARTPTS",
                *ENCODE_ARGS,
                str(out),
            ],
            log_path,
            f"step 7: cold open glimpse {i + 1}",
        )
        glimpses.append(out)
    return glimpses


def concat_timeline(
    parts: list[Path],
    work: Path,
    log_path: Path,
    logo: Path | None = None,
    branding: dict | None = None,
    frame: Frame = LANDSCAPE,
) -> Path:
    """All parts in order -> work/timeline.mp4. Without a logo it's a stream
    copy; with one, the video is encoded once more with the logo on top
    (same video settings as ENCODE_ARGS, audio copied)."""
    concat_list = work / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
        encoding="utf-8",
    )
    timeline = work / "timeline.mp4"
    if logo is None:
        args = ["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy"]
    else:
        args = [
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-loop", "1", "-i", str(logo),
            "-filter_complex", logo_overlay(branding or {}, frame.w),
            "-map", "[v]", "-map", "0:a",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-r", "60", "-c:a", "copy",
        ]
    run_ffmpeg(
        [*args, str(timeline)],
        log_path,
        "step 7: concatenating segments" + (" + logo" if logo else ""),
    )
    return timeline


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
    frame: Frame = LANDSCAPE,
    final_name: str = "final.mp4",
) -> Path:
    """hook_files: prebuilt cold open glimpses (from build_intro_hook);
    None = build them here (backward compatible). intro_seconds overrides
    the config (intro adjustment for drop sync). music_offset: start the
    music this many seconds INTO the track, with a short fade-in."""
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
    files, _ = branding_files(cfg)  # missing files are flagged by cmd_finish
    _text_card(plan.intro_text, intro_s, intro, log_path, "intro card",
               files["intro_image"], frame)

    # Stats outro when [editing].stats_outro is on and the data exists,
    # otherwise a plain text card (never a half-empty stats card)
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
        # a slightly longer card: three or four lines need reading time
        _stats_card(plan.outro_text, pov_name, stats,
                    max(outro_s, 3.5), outro, log_path, files["outro_image"], frame)
    else:
        _text_card(plan.outro_text, outro_s, outro, log_path, "outro card",
                   files["outro_image"], frame)

    hook = (
        hook_files
        if hook_files is not None
        else build_intro_hook(plan, candidates_by_id, clips_dir, work, cfg, log_path, frame)
    )
    ordered = [*hook, intro, *segment_files, outro]
    timeline = concat_timeline(
        ordered, work, log_path, files["logo"], cfg.get("branding", {}), frame
    )

    total = probe_duration(timeline)
    final = run_dir / final_name

    # SFX layer: whoosh into every clip change, impact on big kills (same
    # times as the impact split, from cut_log). Mixed below the music.
    sfx_inputs: list[str] = []
    sfx_chains: list[str] = []
    sfx_labels: list[str] = []
    if editing.get("sfx", False):
        sfx_dir = Path(editing.get("sfx_dir", "assets/sfx"))
        if not sfx_dir.is_absolute():
            # a relative path is anchored in the project folder, not the CWD
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
    # music_offset > 0 → the track starts partway in (so the drop hits the
    # climax); short fade-in so the jump-in doesn't pop
    off = max(0.0, float(music_offset))
    # a track shorter than the film fades out where it ends instead of
    # cutting off mid-bar (cmd_finish flags it in the report)
    music_end = min(total, probe_duration(music_path) - off)
    fade_start = max(0.0, music_end - 3.0)
    music_chain = (
        f"[1:a]atrim={off:.3f}:{off + total:.3f},asetpts=PTS-STARTPTS,"
        + ("afade=t=in:st=0:d=0.8," if off > 0 else "")
        + f"afade=t=out:st={fade_start:.3f}:d=3"
    )
    if editing.get("sidechain_duck", False):
        # Game audio is ducked dynamically by the music's actual level
        # (sidechaincompress) instead of a fixed volume. When the music fades
        # out at the end, the game audio comes back up on its own.
        thr = float(editing.get("sidechain_threshold", 0.05))
        ratio = float(editing.get("sidechain_ratio", 8.0))
        release = float(editing.get("sidechain_release_ms", 300))
        mix = (
            f"[0:a]volume={game_vol}[game];"
            f"{music_chain},asplit=2[m1][m2];"
            f"[game][m2]sidechaincompress=threshold={thr}:ratio={ratio}"
            f":attack=20:release={release}[gduck];"
            f"[gduck][m1]{sfx_in}amix=inputs={n_mix}"
            f":duration=first:normalize=0[amix]"
        )
    else:
        mix = (
            f"[0:a]volume={game_vol}[game];"
            f"{music_chain}[music];"
            f"[game][music]{sfx_in}amix=inputs={n_mix}"
            f":duration=first:normalize=0[amix]"
        )
    if sfx_chains:
        mix = ";".join(sfx_chains) + ";" + mix
    run_ffmpeg(
        [
            "-i", str(timeline),
            "-i", str(music_path),
            *[a for p in sfx_inputs for a in ("-i", p)],
            "-filter_complex", mix,
            "-map", "0:v", "-map", "[amix]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
            str(final),
        ],
        log_path,
        "step 7: music mix + export",
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
    thumbnail: Path | None = None,
) -> Path:
    final_duration = probe_duration(final_path)
    lines = [
        f"# Fragsmith report — {Path(plan.demo_path).name}",
        "",
        f"**Generated:** {datetime.now():%Y-%m-%d %H:%M} by Fragsmith {__version__}",
        f"**Film:** {final_path.name} ({final_duration:.1f} s)",
        f"**Style:** {plan.style}",
        f"**Music:** {plan.music_track} ({beats.get('bpm', '?')} BPM)",
    ]
    if thumbnail:
        lines.append(f"**Thumbnail:** {thumbnail.name}")
    # the drop sync decision (put in the beats dict by cmd_finish)
    ds = beats.get("drop_sync")
    if ds:
        method = (
            "intro adjustment"
            if ds["method"] == "intro"
            else f"music offset {ds['music_offset']:.1f} s"
        )
        err = ds.get("actual_error_ms", ds.get("predicted_error_ms", 0))
        lines.append(
            f"**Drop sync:** climax ({ds['climax_clip_id']}) placed on "
            f"the drop at {ds['drop_track_s']:.1f} s into the track via {method} — "
            f"offset {err:+d} ms"
        )
    # the same numbers the stats outro shows, so they can be checked here
    any_cand = next(iter(candidates_by_id.values()), None)
    stats = player_stats(
        events, getattr(any_cand, "player_steamid", ""), candidates_by_id
    ) if any_cand else None
    if stats:
        line = (
            f"**POV stats:** {stats['kills']} kills / {stats['deaths']} deaths "
            f"(K/D {stats['kd']:.2f})"
        )
        if stats["hs_pct"] is not None:
            line += f", {stats['hs_pct']}% HS"
        if stats["best_clutch"]:
            line += f", best clutch 1v{stats['best_clutch']}"
        lines.append(line)
    lines += [
        "",
        "## Clips in the film",
        "",
        "| # | clip | hype | length | overlay | beat sync (target ±100 ms) |",
        "|---|------|------|--------|---------|----------------------------|",
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
            f"{f'{offset:+d} ms' if offset is not None else 'skipped'} |"
        )
    lines += ["", "## Director's reasoning", ""]
    for d in sorted(plan.decisions, key=lambda d: d.order):
        # show the effect choices the Director made beyond the default rules
        choices = []
        if d.is_climax:
            choices.append("climax")
        choices += [f"+{e}" for e in d.effects_on]
        choices += [f"−{e}" for e in d.effects_off]
        suffix = f" *(Director's choices: {', '.join(choices)})*" if choices else ""
        lines.append(f"- **{d.clip_id}:** {d.reasoning}{suffix}")
    if plan.dropped_clip_ids:
        lines += ["", "## Dropped candidates", ""]
        for cid in plan.dropped_clip_ids:
            cand = candidates_by_id.get(cid)
            score = f" (hype {cand.hype_score})" if cand else ""
            lines.append(f"- {cid}{score}")
    if flags:
        lines += ["", "## Flags", ""]
        lines += [f"- {f}" for f in flags]
    lines += [
        "",
        "## Cut log (beat sync)",
        "",
        "```json",
        json.dumps(cut_log, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    report = run_dir / "report.md"
    report.write_text("\n".join(lines), encoding="utf-8")
    return report
