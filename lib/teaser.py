"""A teaser next to the film (`finish --teaser`): about 30 s, landscape.

The user's brief (2026-11-04): the drop about in the middle, quick cuts, and
the slow-mo and rewind. So:
- before the drop, the weaker kills, each cut shorter than the last, so they
  speed up into the drop
- the climax kill like the velocity cold open's last kill (in, slow-mo,
  rewound, then at normal speed), with that normal-speed kill on the drop
- after the drop, the stronger kills in quick, faster cuts
- a short end card

A match has a limited number of kills: when there aren't enough to fill 30 s,
the teaser is shorter, and the drop stays about in the middle.
"""
from __future__ import annotations

from pathlib import Path

from lib.assemble import (
    _ensure_sfx,
    _text_card,
    _velocity_last,
    concat_timeline,
)
from lib.branding import branding_files
from lib.edit_engine import ENCODE_ARGS, LANDSCAPE, Frame, _atempo_chain, _climax_clip_id
from lib.ffmpeg_util import probe_duration, run_ffmpeg
from lib.models import EditPlan

TEASER_S = 30.0
CARD_S = 1.5
PRE_SPEED = 1.25     # before the drop: a little fast
POST_SPEED = 1.5     # after it: faster
PIECE_MIN_S = 0.45   # a cut is never shorter than this ...
PIECE_MAX_S = 2.5    # ... or longer than this
KILL_AT = 0.7        # where in its cut a kill lands
MERGE_S = 3.0        # kills in one clip closer than this are one cut
MUSIC_FADE_OUT_S = 1.5
GAME_VOLUME = 0.3


def _kills(plan: EditPlan, candidates_by_id: dict, clips_dir: Path) -> tuple[tuple | None, list]:
    """(the hero kill, the others by rising score): (score, clip, offset).
    The hero is the climax clip's last kill. Kills in one clip closer than
    MERGE_S are one cut (a double or triple kill, on its last kill), so no
    moment shows twice; the ones just before the hero are in its rewind."""
    climax = _climax_clip_id(plan)
    hero, others = None, []
    for d in plan.decisions:
        cand = candidates_by_id.get(d.clip_id)
        clip = clips_dir / f"{d.clip_id}.mp4"
        kills = getattr(cand, "kills", None) if cand else None
        if not kills or not clip.exists():
            continue
        groups: list[list] = []
        for k in sorted(kills, key=lambda k: k.offset_s):
            if groups and k.offset_s - groups[-1][-1].offset_s < MERGE_S:
                groups[-1].append(k)
            else:
                groups.append([k])
        for i, g in enumerate(groups):
            score = cand.hype_score + max(0.5 if k.headshot else 0.0 for k in g) + 0.5 * (len(g) - 1)
            if d.clip_id == climax and i + 1 == len(groups):
                hero = (score + 100.0, clip, g[-1].offset_s)
            else:
                others.append((score, clip, g[-1].offset_s))
    if hero is None and others:
        others.sort(key=lambda p: p[0])
        hero = others.pop()
    return hero, sorted(others, key=lambda p: p[0])


def cut_lengths(n: int, total: float, first: float, last: float) -> list[float]:
    """n cut lengths that fill total, each a set share from first down to
    last (so the cuts speed up), within PIECE_MIN_S-PIECE_MAX_S. They add up
    to less than total when n cuts can't fill it."""
    if n <= 0 or total <= 0:
        return []
    weights = [first + (last - first) * (i / (n - 1) if n > 1 else 0) for i in range(n)]
    scale = total / sum(weights)
    return [min(PIECE_MAX_S, max(PIECE_MIN_S, w * scale)) for w in weights]


def _cut(clip: Path, offset: float, length: float, speed: float, out: Path,
         log_path: Path, frame: Frame) -> None:
    """One quick cut: length seconds of film at speed, the kill at KILL_AT."""
    src = length * speed
    start = max(0.0, offset - KILL_AT * src)
    run_ffmpeg(
        [
            "-ss", f"{start:.3f}", "-t", f"{src:.3f}", "-i", str(clip),
            "-vf", f"{frame.normalize()},setpts=(PTS-STARTPTS)/{speed},"
                   "fade=t=in:st=0:d=0.04:color=white",
            "-af", f"asetpts=PTS-STARTPTS,{_atempo_chain(speed)}",
            *ENCODE_ARGS,
            str(out),
        ],
        log_path,
        f"teaser: cut {out.stem}",
    )


def build_teaser(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    music_path: Path,
    drop_track_s: float | None,
    run_dir: Path,
    cfg: dict,
    log_path: Path,
) -> tuple[Path, dict]:
    """teaser.mp4 in run_dir, and what's in it: {"seconds", "drop_s",
    "kills"}. drop_track_s: where the track's drop is (None: the music
    starts at the beginning of the track)."""
    frame = LANDSCAPE
    work = run_dir / "04-segments-teaser"
    work.mkdir(parents=True, exist_ok=True)
    hero, others = _kills(plan, candidates_by_id, clips_dir)
    if hero is None:
        raise ValueError("no kills in the rendered clips")

    # the climax kill: its normal-speed kill goes on the drop
    hero_file = work / "10_hero.mp4"
    hero_kill = _velocity_last(hero[1], hero[2], hero_file, log_path, frame)
    hero_len = probe_duration(hero_file)

    # the weaker half before the drop, the stronger after it
    n_pre = (len(others) + 1) // 2
    pre_kills, post_kills = others[:n_pre], others[n_pre:]
    middle = TEASER_S / 2
    pre = cut_lengths(len(pre_kills), middle - hero_kill, 1.6, 0.6)
    post_room = TEASER_S - middle - (hero_len - hero_kill) - CARD_S
    post = cut_lengths(len(post_kills), post_room, 1.0, 0.6)
    # too few kills: shorten the longer side so the drop stays in the middle
    before, after = sum(pre) + hero_kill, (hero_len - hero_kill) + sum(post) + CARD_S
    if after < before and pre:
        pre = cut_lengths(len(pre_kills), max(0.0, after - hero_kill), 1.6, 0.6)

    parts: list[Path] = []
    for i, ((_, clip, off), length) in enumerate(zip(pre_kills, pre)):
        out = work / f"00_pre_{i:02d}.mp4"
        _cut(clip, off, length, PRE_SPEED, out, log_path, frame)
        parts.append(out)
    parts.append(hero_file)
    for i, ((_, clip, off), length) in enumerate(zip(post_kills, post)):
        out = work / f"20_post_{i:02d}.mp4"
        _cut(clip, off, length, POST_SPEED, out, log_path, frame)
        parts.append(out)
    files, _ = branding_files(cfg)
    card = work / "30_card.mp4"
    _text_card(plan.outro_text, CARD_S, card, log_path, "teaser end card",
               files["outro_image"], frame)
    parts.append(card)

    timeline = concat_timeline(parts, work, log_path, files["logo"], cfg.get("branding", {}), frame)
    total = probe_duration(timeline)
    drop_s = sum(probe_duration(p) for p in parts[: len(pre)]) + hero_kill

    # The music: the drop where the hero kill is, a boom on it, the game
    # sound under the music, a fade-out at the end
    start = max(0.0, (drop_track_s or drop_s) - drop_s)
    editing = cfg.get("editing", {})
    sfx_dir = Path(editing.get("sfx_dir", "assets/sfx"))
    if not sfx_dir.is_absolute():
        sfx_dir = Path(__file__).resolve().parent.parent / sfx_dir
    boom = _ensure_sfx(sfx_dir, log_path)["boom"]
    out = run_dir / "teaser.mp4"
    run_ffmpeg(
        [
            "-i", str(timeline), "-i", str(music_path), "-i", str(boom),
            "-filter_complex",
            f"[1:a]atrim={start:.3f}:{start + total:.3f},asetpts=PTS-STARTPTS,"
            + ("afade=t=in:d=0.3," if start > 0 else "")
            + f"afade=t=out:st={max(0.0, total - MUSIC_FADE_OUT_S):.3f}:d={MUSIC_FADE_OUT_S}[m];"
            f"[0:a]volume={GAME_VOLUME}[g];"
            f"[2:a]adelay=delays={int(drop_s * 1000)}:all=1[b];"
            "[m][g][b]amix=inputs=3:duration=first:normalize=0,"
            "alimiter=limit=0.95:level=false:latency=true[a]",
            "-map", "0:v", "-map", "[a]", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "256k", "-shortest",
            str(out),
        ],
        log_path,
        "teaser: music mix + export",
    )
    return out, {"seconds": round(total, 2), "drop_s": round(drop_s, 2),
                 "kills": len(pre) + 1 + len(post)}
