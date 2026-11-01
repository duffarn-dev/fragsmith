"""Thumbnail: a 1280x720 JPG of the climax kill, ready for YouTube.

The frame comes from the raw climax clip just before its last kill (so the
enemy is still in the picture), not from final.mp4: the film's own overlays
(counter, lower third, letterbox) would clash with the big thumbnail text.
It gets the film's grade and LUT, a slight push-in, the climax label, and the
[branding] logo. A thumbnail that fails never fails the film.
"""
from __future__ import annotations

from pathlib import Path

from lib.branding import PROJECT_DIR, branding_files, logo_overlay
from lib.edit_engine import _climax_clip_id, _effects_filters
from lib.ffmpeg_util import FONT_FILE, FfmpegError, drawtext_escape, run_ffmpeg
from lib.models import EditPlan

THUMB_W, THUMB_H = 1280, 720
MAX_BYTES = 2 * 1024 * 1024  # YouTube's thumbnail limit
PRE_KILL_S = 0.1  # a moment before the kill: the enemy hasn't dropped yet
PUSH_IN = 1.15


def climax_label(decision, cand) -> str:
    """The Director's overlay text if there is one, otherwise ACE / 4K / 1v3."""
    if decision.overlay_text:
        return decision.overlay_text
    if cand.kind == "ace":
        return "ACE"
    if cand.clutch_vs and cand.clutch_vs >= 2:
        return f"1v{cand.clutch_vs}"
    if cand.kill_count >= 2:
        return f"{cand.kill_count}K"
    return ""


def pick_moment(plan: EditPlan, candidates_by_id: dict, clips_dir: Path):
    """(clip path, source time, label) for the climax kill, or None."""
    climax_id = _climax_clip_id(plan)
    decision = next((d for d in plan.decisions if d.clip_id == climax_id), None)
    cand = candidates_by_id.get(climax_id)
    clip = clips_dir / f"{climax_id}.mp4"
    if decision is None or cand is None or not clip.exists():
        return None
    kills = getattr(cand, "kills", None)
    t = kills[-1].offset_s - PRE_KILL_S if kills else cand.duration_seconds / 2
    return clip, max(0.0, t), climax_label(decision, cand)


def _font_size(label: str) -> int:
    # Arial Bold capitals are ~0.68 em wide: keep the label inside ~90% of the width
    return min(130, int(THUMB_W * 0.9 / 0.68 / max(1, len(label))))


def make_thumbnail(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    out_path: Path,
    cfg: dict,
    log_path: Path,
) -> Path | None:
    moment = pick_moment(plan, candidates_by_id, clips_dir)
    if moment is None:
        return None
    clip, t, label = moment

    # the film's look, with the LUT anchored like build_segments does it (a
    # missing LUT was already flagged there)
    fx = dict(cfg.get("effects", {}))
    lut = str(fx.get("lut", "") or "")
    if lut:
        lut_path = Path(lut) if Path(lut).is_absolute() else PROJECT_DIR / lut
        fx["lut"] = str(lut_path) if lut_path.is_file() else ""
    look = ",".join(_effects_filters(fx, None))
    chain = (
        f"crop=iw/{PUSH_IN}:ih/{PUSH_IN},scale={THUMB_W}:{THUMB_H}"
        + (f",{look}" if look else "")
    )
    if label:
        size = _font_size(label)
        chain += (
            f",drawtext=fontfile='{FONT_FILE}':text='{drawtext_escape(label)}'"
            f":expansion=none:fontsize={size}:fontcolor=white"
            f":borderw={max(4, size // 14)}:bordercolor=black"
            f":x=48:y=h-text_h-56"
        )

    to_jpeg = "scale=out_range=full,format=yuvj420p"  # JPEG is full-range yuv
    files, _ = branding_files(cfg)
    logo = files["logo"]
    args = ["-ss", f"{t:.3f}", "-i", str(clip)]
    if logo:
        # looped, as logo_overlay requires: a kill time between two frames
        # made the unlooped logo end first, and ffmpeg exited 0 with no jpg
        args += ["-loop", "1", "-i", str(logo)]
        graph = f"[0:v]{chain}[bg];" + logo_overlay(
            cfg.get("branding", {}), THUMB_W, main="[bg]", logo="[1:v]", out="[lv]"
        ) + f";[lv]{to_jpeg}[v]"
        args += ["-filter_complex", graph, "-map", "[v]"]
    else:
        args += ["-vf", f"{chain},{to_jpeg}"]

    for quality in (2, 5):  # a busy frame can pass 2 MB at the best quality
        run_ffmpeg(
            [*args, "-frames:v", "1", "-q:v", str(quality), str(out_path)],
            log_path,
            "step 7: thumbnail",
        )
        if not out_path.exists():
            raise FfmpegError("ffmpeg exited without writing the thumbnail")
        if out_path.stat().st_size <= MAX_BYTES:
            break
    return out_path
