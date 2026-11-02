"""Step 6: editing engine. Slow-mo/speed ramps, overlays, beat-aligned
clip lengths. Raw ffmpeg via subprocess (a deliberate choice over moviepy).

Input:  raw clips (03-raw-clips/<clip_id>.mp4) + edit plan + beats
Output: 04-segments/NN_<clip_id>.mp4 (uniform 60 fps h264 in the output
        frame, 1920x1080 unless a Frame says otherwise, + 48k stereo)
        + a cut log with the beat offset per cut (the ±100 ms target is
        checked by ear; the numbers are logged so they can be compared)
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import NamedTuple

from lib.beats import nearest_beat_before, shift_beats
from lib.ffmpeg_util import (
    FONT_FILE,
    drawtext_escape,
    probe_duration,
    probe_fps,
    run_ffmpeg,
)
from lib.hype_score import LONG_SHOT_M
from lib.models import EditDecision, EditPlan

ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-r", "60",
    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
]


class Frame(NamedTuple):
    """The output frame every segment, card and effect is built for.

    cover=False scales the source to fit and pads (a 16:9 render into a 16:9
    frame: a no-op). cover=True fills the frame and crops the center: a 9:16
    film from a 16:9 render keeps the crosshair, and so the action, in view.
    """

    w: int
    h: int
    cover: bool = False

    @property
    def size(self) -> str:
        return f"{self.w}x{self.h}"

    @property
    def portrait(self) -> bool:
        """Shorts/TikTok: captions and buttons cover the bottom ~20% and the
        app bar the top ~8%, so overlays keep out of those bands."""
        return self.h > self.w

    def normalize(self) -> str:
        """Filters that bring any source into this frame (fps not included)."""
        if self.cover:
            return (
                f"scale={self.w}:{self.h}:force_original_aspect_ratio=increase,"
                f"crop={self.w}:{self.h}"
            )
        return (
            f"scale={self.w}:{self.h}:force_original_aspect_ratio=decrease,"
            f"pad={self.w}:{self.h}:(ow-iw)/2:(oh-ih)/2"
        )


LANDSCAPE = Frame(1920, 1080)
PORTRAIT = Frame(1080, 1920, cover=True)


def _atempo_chain(speed: float) -> str:
    """atempo supports 0.5-2.0 per link, so chain it for lower/higher factors."""
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
    """Split the clip into (start, end, speed) parts covering the whole duration.

    extra_slow: extra (start, end, factor) markers outside the Director's
    plan, used by the micro slow-mo on headshots.
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
        start = max(start, cursor)  # overlap: a later segment doesn't win backward
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
    """Map a point in time in the SOURCE clip to time in the output timeline.

    KillMark.offset_s is in source time; slow-mo/ramps stretch the timeline,
    so a kill at 5.0 s in the source lands later in the output when
    something before it is in slow-mo. Effects that must hit a kill (freeze,
    headshot punch, kill counter, impact split) MUST map the offset through
    this, with the same parts that _build_filter got.
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
    """Split parts at source time t and return (parts, freeze insert index).

    The index is how many parts lie BEFORE t; the freeze-frame chain in
    _build_filter is inserted there. If t is < 0.02 s from an existing
    boundary, nothing is split (sub-segments under ~1 frame are useless).
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


RAMP_STEPS = 3      # sub-segments per speed transition (2-4 works well)
RAMP_HALF_S = 0.15  # source time taken from each side of the boundary
MIN_STEP_S = 0.04   # a ramp step must not be < ~2 frames at 60 fps


def _eased_parts(
    parts: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Smooth speed ramps: replace hard speed jumps with short interpolated
    ramp steps around the boundary (e.g. 1.0 → 0.85 → 0.7 → 0.55 → 0.4).

    Takes up to RAMP_HALF_S from each side (max 40% of the neighbor part's
    length). If the steps get too short (< MIN_STEP_S), the boundary stays hard.
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
    effects: dict, beat_ctx: dict | None, frame: Frame = LANDSCAPE
) -> list[str]:
    """Effect chain (before text overlays): grade, beat zoom, wobble, vignette.

    Everything is scaled by [effects].intensity. Beat zoom uses the music's
    actual BPM/phase so the pulse hits the beats.
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

    # LUT grading (.cube); existence is checked in build_segments (flag)
    lut = str(effects.get("lut", "") or "")
    if lut and k > 0:
        lut_path = Path(lut).as_posix().replace(":", "\\:")
        vf.append(f"lut3d=file='{lut_path}'")

    # film grain: temporal noise, scaled by intensity
    grain = max(0.0, float(effects.get("grain", 0.0)))
    if grain > 0 and k > 0:
        vf.append(f"noise=alls={min(100.0, grain * 12 * k):.1f}:allf=t")

    if effects.get("beat_zoom", True) and beat_ctx and k > 0:
        amp = 0.035 * k * float(effects.get("beat_zoom_strength", 1.0))
        p = beat_ctx["period"]
        ph = beat_ctx["phase"]
        # time since the previous beat: u = mod(t - phase + P, P); a zoom
        # pulse that hits on the beat and decays exponentially
        expr = (
            f"1+{amp:.4f}*exp(-7*mod(on/60-{ph:.3f}+{p:.4f},{p:.4f})/{p:.4f})"
        )
        vf.append(
            f"zoompan=z='{expr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d=1:fps=60:s={frame.size}"
        )

    if effects.get("wobble", False) and k > 0:
        period4 = (beat_ctx["period"] * 4) if beat_ctx else 2.0
        vf.append(
            f"rotate=a='{0.012 * k:.4f}*sin(2*PI*t/{period4:.3f})':ow=iw:oh=ih:c=black"
        )

    if effects.get("vignette", True) and k > 0:
        vf.append("vignette=PI/5")
    return vf


def _max_tilt_degrees(zoom: float, frame: Frame = LANDSCAPE) -> float:
    """Largest tilt angle where the zoom crop covers the black corners from
    rotate: cos(θ) + (long/short side)·sin(θ) <= zoom, minus a 1° margin.
    The long side binds, so 16:9 and 9:16 get the same limit."""
    ratio = max(frame.w, frame.h) / min(frame.w, frame.h)
    r = math.hypot(1.0, ratio)
    if zoom >= r:
        return 45.0
    return max(0.0, math.degrees(math.atan(ratio) - math.acos(zoom / r)) - 1.0)


def _kill_punch_filters(
    kill_fx: dict, effects: dict, frame: Frame = LANDSCAPE
) -> list[str]:
    """Deep zoom into the crosshair (frame center) at the moment of the kill,
    optionally combined with a dutch tilt. Times are in OUTPUT time (via
    source_to_output_time).

    Envelope: the zoom frames the kill. Full ~0.05 s BEFORE the kill, held
    ~0.4 s, snaps out in ~0.18 s. The tilt follows the zoom with a steeper
    curve (^3.5 vs ^2) so the crop always covers the rotation corners.
    """
    t_kill = kill_fx["t"]
    zmax = max(1.1, float(effects.get("kill_zoom_factor", 1.7)))
    dz = zmax - 1.0
    # if a freeze frame falls on the same kill, the zoom is held through the
    # whole freeze (hold_until) before snapping out; otherwise a 0.4 s hold
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
    deg = min(float(kill_fx.get("tilt_deg", 0.0)), _max_tilt_degrees(zmax, frame))
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
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=60:s={frame.size}"
    )
    return vf


def _headshot_punch_filter(
    punches: list[dict], _effects: dict, frame: Frame = LANDSCAPE
) -> str:
    """Small zoom punch on headshots: ease in ~0.35 s to max (< ~1.25×), a
    short hold over the kill, snap out in 2-3 frames. All punches are
    combined in ONE zoompan (each envelope is 0 outside its window), centered
    on the crosshair. Times are in OUTPUT time (via source_to_output_time +
    any freeze shift)."""
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
        f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:fps=60:s={frame.size}"
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
    frame: Frame = LANDSCAPE,
    tags: list[tuple[float, str]] | None = None,
) -> tuple[str, str, str]:
    """filter_complex: trim/setpts/atempo per part + concat + effects +
    overlay + entry transition. src_fps: the source frame rate; high-fps
    sources are slowed down with real frames, without minterpolate."""
    punch = effects.get("enabled", True) and effects.get("slowmo_punch", True)
    smooth = (
        effects.get("enabled", True)
        and effects.get("smooth_slowmo", False)
        and src_fps <= HIGHFPS_THRESHOLD  # real frames beat interpolation
    )
    k = max(0.0, float(effects.get("intensity", 1.0)))

    chains: list[str] = []
    labels: list[tuple[str, str]] = []  # (video, audio) in concat order
    for i, (start, end, speed) in enumerate(parts):
        vchain = (
            f"[0:v]trim=start={start:.3f}:end={end:.3f},"
            f"setpts=(PTS-STARTPTS)/{speed:.4f}"
        )
        if smooth and speed < 0.95:
            # Optical flow interpolation → smooth slow-mo instead of
            # duplicated frames. Only on slow parts: minterpolate is slow
            # and causes artifacts on smoke/particles (see learnings_runtime.md).
            vchain += ",minterpolate=fps=60:mi_mode=mci"
        # normalize resolution AND fps BEFORE concat: the punch zoompan locks
        # its part to the frame size, concat requires the same size, and all
        # zoompan expressions after concat treat time as on/60 (sources ≠ 60
        # fps would otherwise get the effects at half/double speed)
        vchain += f",fps=60,{frame.normalize()}"
        # micro slow-mo parts have their own headshot punch; a push-in on
        # top would stack two zooms on the same kill
        in_micro = any(
            start >= ms - 1e-3 and end <= me + 1e-3 for ms, me in punch_skip
        )
        # the length requirement keeps push-in off short ramp steps from
        # _eased_parts (a zoom restarting per step would cause a visible jerk)
        if punch and speed < 1.0 and k > 0 and (end - start) >= 0.25 and not in_micro:
            # push-in during slow-mo: slow zoom from 1.02 to ~1.02+0.12*k
            out_dur = max(0.1, (end - start) / speed)
            rate = (0.12 * k) / (out_dur * 60)
            vchain += (
                f",zoompan=z='1.02+{rate:.6f}*on'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d=1:fps=60:s={frame.size}"
            )
        chains.append(f"{vchain}[v{i}]")
        chains.append(
            f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
            f"{_atempo_chain(speed)}[a{i}]"
        )
        labels.append((f"[v{i}]", f"[a{i}]"))

    if freeze:
        # Freeze frame: take one frame at the moment of the kill, clone it for
        # the freeze duration (tpad), silent audio underneath. Inserted into
        # the concat order at after_idx (from _split_parts_at) so it lands
        # ON the kill.
        ft, fdur = freeze["src_t"], freeze["dur"]
        chains.append(
            f"[0:v]trim=start={ft:.3f}:end={ft + 0.1:.3f},setpts=PTS-STARTPTS,"
            f"select='eq(n,0)',tpad=stop_mode=clone:stop_duration={fdur:.3f},"
            f"fps=60,{frame.normalize()}[vfz]"
        )
        chains.append(
            f"aevalsrc=0|0:s=48000:d={fdur:.3f},"
            f"aformat=sample_fmts=fltp:channel_layouts=stereo[afz]"
        )
        labels.insert(int(freeze["after_idx"]), ("[vfz]", "[afz]"))

    inputs = "".join(v + a for v, a in labels)
    chains.append(f"{inputs}concat=n={len(labels)}:v=1:a=1[vc][ac]")

    # (resolution is already normalized per part, before concat)
    vf = _effects_filters(effects, beat_ctx, frame)
    if hs_punches:
        # headshot punch before kill punch: both are center zooms, and the
        # selection in build_segments makes sure the windows don't overlap
        vf.append(_headshot_punch_filter(hs_punches, effects, frame))
    if kill_fx:
        # kill punch BEFORE text/letterbox: the text shouldn't rotate along,
        # and the letterbox bars should stay still over the tilted image
        vf += _kill_punch_filters(kill_fx, effects, frame)
    if impacts:
        # 3 frames of chroma shift on big kills: "impact".
        # chromashift, NOT rgbashift: rgbashift forces YUV→RGB→YUV on the
        # WHOLE clip (format negotiation happens regardless of enable) and
        # loses color everywhere; chromashift works in native yuv420p. After
        # the kill punch (shifts the zoomed image), before the text layers.
        shift = max(2, round(5 * k))
        cond = "+".join(f"between(t,{t:.3f},{t + 0.05:.3f})" for t in impacts)
        vf.append(f"chromashift=cbh=-{shift}:crh={shift}:enable='{cond}'")
    if decision.overlay_text:
        text = drawtext_escape(decision.overlay_text)
        if effects.get("enabled", True) and effects.get("animated_overlays", False):
            # the text slides up and fades in (0.4-0.65 s), fades out toward
            # 3.2 s, instead of a hard on/off
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
        # Kill counter that ticks up on every kill (output time) with an
        # alpha pop per tick. Left side: doesn't collide with the killfeed
        # (top right), overlay_text (center) or the lower third (bottom left).
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
    if tags:
        # Kill tags (NOSCOPE, WALLBANG, ...) pop in just under the crosshair
        # on their kill and fade after ~1 s: amber, so they read as a callout,
        # not as the overlay text (center, 72%) or the counter (left)
        for start, label in tags:
            vf.append(
                f"drawtext=fontfile='{FONT_FILE}'"
                f":text='{drawtext_escape(label)}':expansion=none"
                f":fontsize=46:fontcolor=0xFFB347:borderw=4:bordercolor=black"
                f":x=(w-text_w)/2:y=h*0.57"
                f":enable='between(t,{start:.3f},{start + 1.1:.3f})'"
                f":alpha='if(lt(t-{start:.3f},0.1),(t-{start:.3f})/0.1,"
                f"if(lt(t-{start:.3f},0.85),1,max(0,(1.1-(t-{start:.3f}))/0.25)))'"
            )
    if lowerthird:
        # Subtle context line (map · round · 1vX) in the bottom left, short
        # fade in/out. Raised above the bottom bar when letterbox is active,
        # and above the captions/buttons band in portrait.
        if frame.portrait:
            y = round(frame.h * 0.78)
        else:
            y = frame.h - (210 if letterbox else 70)
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
        # white flash into the clip where the Director chose cut/speed_ramp
        vf.append("fade=t=in:st=0:d=0.13:color=white")
    if not vf:
        vf = ["null"]  # an empty filter chain is invalid in filter_complex
    if letterbox:
        # (Adjusted 2026-07-05) ONLY a bottom bar (160 px), slides in 0.4 s.
        # The top bar covered the killfeed, so kills through smoke/walls
        # became unreadable. Done with overlay, NOT drawbox: in drawbox
        # expressions `t` means thickness, not time; only overlay has a
        # time-animated position.
        # Note: overlay_text sits at y=h*0.72 (~777 in 1080p) and clears the
        # bar (920).
        slide = "min(160,160*t/0.4)"
        chains.append(f"[vc]{','.join(vf)}[vfx]")
        chains.append(f"color=black:s={frame.w}x160:r=60[lbb]")
        chains.append(f"[vfx][lbb]overlay=x=0:y='{frame.h}-{slide}'[vout]")
    else:
        chains.append(f"[vc]{','.join(vf)}[vout]")
    return ";".join(chains), "[vout]", "[ac]"


# Instant replay: rewind length, slow-mo window around the kill, and factor
REPLAY_REWIND_S = 1.0    # length of the rewind snippet in the output
REPLAY_PRE_S = 1.8       # source seconds BEFORE the kill in the replay
REPLAY_POST_S = 1.2      # source seconds AFTER the kill
REPLAY_FACTOR = 0.45     # playback speed in the replay


def pick_replay_clip(plan: EditPlan, candidates_by_id: dict, cfg: dict):
    """Which clip gets an instant replay of its last kill. Returns
    (clip_id, KillMark) or None.

    The Director can choose explicitly via effects_on=["replay"] (max one,
    validated). Otherwise: the climax clip, if it's big enough (3+ kills
    or a clutch) and doesn't have effects_off=["replay"]. Used BOTH by the
    render side (victim-POV angle) and editing (the replay segment); they
    MUST agree on the choice.
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
            return None  # a replay on a weak clip is worse than no replay
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
    frame: Frame = LANDSCAPE,
) -> float:
    """Build rewind + slow-mo replay as a separate segment.

    main_seg: the finished climax segment (the rewind runs from the output
    end back to the kill). replay_src: the victim-POV clip (_alt) or the
    same POV as fallback; same tick range, so kill_offset_s applies to both.
    Returns the actual duration.
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
        and probe_fps(replay_src) <= HIGHFPS_THRESHOLD
    )
    replay_v = (
        f"[1:v]trim=start={w0:.3f}:end={w1:.3f},"
        f"setpts=(PTS-STARTPTS)/{REPLAY_FACTOR:.3f}"
    )
    if smooth:
        replay_v += ",minterpolate=fps=60:mi_mode=mci"
    replay_v += f",fps=60,{frame.normalize()}"
    vf = _effects_filters(effects, None, frame)
    if vf:
        replay_v += "," + ",".join(vf)
    # REPLAY badge: top center (the killfeed lives in the top right), pulsing;
    # in portrait below the app bar
    badge_y = round(frame.h * 0.12) if frame.portrait else 70
    replay_v += (
        f",drawtext=fontfile='{FONT_FILE}':text='REPLAY':expansion=none"
        f":fontsize=72:fontcolor=white:borderw=5:bordercolor=black"
        f":x=(w-text_w)/2:y={badge_y}:alpha='0.6+0.4*abs(sin(t*3))'"
    )

    chains = [
        # rewind: from just before the kill to the segment end, reversed at high speed
        f"[0:v]trim=start={rew_from:.3f}:end={seg_duration:.3f},"
        f"setpts=PTS-STARTPTS,reverse,setpts=PTS/{rew_speed:.4f},fps=60,"
        f"{frame.normalize()}[rv]",
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


def kill_tag(km) -> str | None:
    """The callout for a kill: up to two details, most special first
    ("NOSCOPE · THROUGH SMOKE"), a long shot as "42 M"."""
    labels = [
        text for flag, text in (
            ("noscope", "NOSCOPE"), ("wallbang", "WALLBANG"),
            ("through_smoke", "THROUGH SMOKE"), ("jumpshot", "JUMPSHOT"),
            ("blind", "BLIND"),
        )
        if getattr(km, flag, False)
    ]
    distance = float(getattr(km, "distance_m", 0.0) or 0.0)
    if distance >= LONG_SHOT_M:
        labels.append(f"{distance:.0f} M")
    return "  ·  ".join(labels[:2]) or None


def _choose(name: str, default: bool, d_on: set, d_off: set) -> bool:
    """The Director's per-clip choices. effects_off wins, effects_on
    overrides the default RULE, but never the config master switch."""
    if name in d_off:
        return False
    if name in d_on:
        return True
    return default


def _timing_plan(decision: EditDecision, cand, duration: float, fx: dict) -> dict:
    """Everything that affects the segment's LENGTH, in one place: speed parts
    (Director slow-mo + micro slow-mo + ramps), freeze frame and out_duration.

    Used by BOTH build_segments and the drop-sync simulation. If this were
    computed separately in two places, the simulated and actual timelines
    would drift apart.
    """
    fx_on = fx.get("enabled", True)
    kills = getattr(cand, "kills", None) if cand else None
    k = max(0.0, float(fx.get("intensity", 1.0)))
    d_on = set(decision.effects_on)
    d_off = set(decision.effects_off)

    kill_zoom_active = fx_on and fx.get("kill_zoom", False) and _choose(
        "kill_zoom", True, d_on, d_off
    )

    # Pick the headshots that get a punch (source time). The last kill is
    # skipped when kill_zoom is on (it already has its own, deeper zoom),
    # and punches closer than 1.5 s apart aren't stacked.
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

    # micro slow-mo around each punch; skip headshots that are already
    # inside a Director slow-mo segment
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
        # must happen BEFORE out_duration: the beat snap and the filter must
        # see exactly the same parts.
        parts = _eased_parts(parts)

    # Freeze frame on the last kill of an ace/big multikill: a "full stop".
    # The duration is counted into out_duration so the beat snap holds.
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


# Sources above this count as high-fps renders: slow-mo then uses real
# frames (setpts) and minterpolate is skipped
HIGHFPS_THRESHOLD = 61.0


def wants_high_fps(decision: EditDecision, cand, fx: dict) -> bool:
    """Should this clip be rendered at high fps? True if anything in the clip
    will be in slow-mo: Director segments or the headshot micro slow-mo.
    (The replay clip is also handled by the render side, since the replay
    slows down the alt angle regardless.)

    Shared by the render side (step 4, BEFORE the clip exists) and the
    phase-stop estimate. Uses cand.duration_seconds since the raw clip
    doesn't exist yet; the same _timing_plan logic step 6 uses on the
    actual duration.
    """
    if decision.slowmo_segments:
        return True
    if cand is None:
        return False
    tp = _timing_plan(decision, cand, float(cand.duration_seconds or 0.0), fx)
    return bool(tp["micro"])


def _climax_clip_id(plan: EditPlan) -> str | None:
    """The Director's is_climax wins, otherwise the last clip."""
    decisions = sorted(plan.decisions, key=lambda d: d.order)
    if not decisions:
        return None
    explicit = [d.clip_id for d in decisions if d.is_climax]
    return explicit[0] if explicit else decisions[-1].clip_id


def _simulate_timeline(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    beats: dict,
    cfg: dict,
    timeline_start: float,
    dur_cache: dict,
) -> tuple[float | None, float]:
    """Predict (where the climax clip starts, where the last segment ends)
    on the timeline, using the same snapping rules as build_segments (pure
    arithmetic + ffprobe, no encoding). The climax start is None if the
    climax raw clip is missing."""
    editing = cfg.get("editing", {})
    fx = cfg.get("effects", {})
    tolerance = float(editing.get("beat_tolerance_seconds", 0.75))
    target_id = _climax_clip_id(plan)
    replay_pick = pick_replay_clip(plan, candidates_by_id, cfg)

    climax_start: float | None = None
    pos = timeline_start
    for d in sorted(plan.decisions, key=lambda d: d.order):
        clip = clips_dir / f"{d.clip_id}.mp4"
        if not clip.exists():
            continue  # build_segments also skips missing clips
        if d.clip_id == target_id:
            climax_start = pos
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
        # the replay segment right after its clip pushes the timeline too
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
    return climax_start, pos


def _simulate_climax_start(
    plan: EditPlan,
    candidates_by_id: dict,
    clips_dir: Path,
    beats: dict,
    cfg: dict,
    timeline_start: float,
    dur_cache: dict,
) -> float | None:
    """Where the climax clip starts (see _simulate_timeline)."""
    return _simulate_timeline(
        plan, candidates_by_id, clips_dir, beats, cfg, timeline_start, dur_cache
    )[0]


def outro_seconds(cfg: dict) -> float:
    """Outro card length as assemble builds it (the stats outro is at least
    3.5 s so its lines can be read)."""
    editing = cfg.get("editing", {})
    outro = float(editing.get("outro_seconds", 2.5))
    return max(outro, 3.5) if editing.get("stats_outro", False) else outro


# The intro card's length is a lever for drop sync. These limits keep the
# card within what looks intentional (not too short, no dead time)
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
    """Pick a drop candidate + adjustment so the climax clip STARTS on the
    music's drop (action and music peak together).

    base_start: the timeline position where the intro card starts (= the
    total length of the cold open glimpses); the intro length is lever 1.
    Two levers, tried in this order per drop candidate (strongest first):
      1. intro adjustment (±): keeps the track's own build-up from the start
      2. music offset (+): start the track further in, for drops far in
    Returns a dict with beats (shifted if needed), intro_seconds,
    music_offset and report info, or None (→ climax-at-the-end behavior).
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

    for drop in drops:  # strongest first
        drop_t = float(drop["time_s"])
        r = drop_t - c0

        # Lever 1: intro adjustment, the drop is close to the natural climax
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

        # Lever 2: music offset, the drop is far INTO the track
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
                # the rest of the track must cover the whole film, or the
                # music stops before the outro (seen: 1:45 of music, 1:54 film)
                _, seg_end = _simulate_timeline(
                    plan, candidates_by_id, clips_dir, shifted, cfg,
                    base_start + base_intro, dur_cache,
                )
                film_end = seg_end + outro_seconds(cfg)
                music_left = float(beats.get("duration_seconds", 0.0)) - off
                if (
                    c is not None
                    and abs(c - (drop_t - off)) <= tolerance
                    and music_left >= film_end
                ):
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
    frame: Frame = LANDSCAPE,
) -> tuple[list[Path], list[dict], list[str]]:
    """Returns (segment files in order, cut log, flags).

    context: optional demo context beyond the candidates, currently only
    {"map": "de_inferno"} (used by the lower third)."""
    editing = cfg.get("editing", {})
    fx = cfg.get("effects", {})
    tolerance = float(editing.get("beat_tolerance_seconds", 0.75))
    # the caller can say where on the timeline the segments actually start
    # (cold open glimpses + any drop-adjusted intro); default is the intro length.
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
    # if the Director explicitly picked a climax, it wins over the
    # "last clip" rule
    climax_ids = {d.clip_id for d in plan.decisions if d.is_climax}

    # A missing LUT file should give a clear flag, not an ffmpeg crash.
    # A relative path is anchored in the project folder (like sfx_dir), not the CWD.
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

    # instant replay: the same pick the render side made
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

        # the Director's per-clip choices (see _choose). The timing logic is
        # shared with the drop-sync simulation via _timing_plan.
        d_on = set(decision.effects_on)
        d_off = set(decision.effects_off)

        def choose(name: str, default: bool) -> bool:
            return _choose(name, default, d_on, d_off)

        tp = _timing_plan(decision, cand, duration, fx)
        parts, freeze, micro = tp["parts"], tp["freeze"], tp["micro"]
        hs_marks, kill_zoom_active = tp["hs_marks"], tp["kill_zoom_active"]
        out_duration = tp["out_duration"]

        def _out_time(t_src: float) -> float:
            """Source → output time, including the shift from an inserted freeze."""
            t = source_to_output_time(t_src, parts)
            if freeze and t_src > freeze["src_t"] + 1e-6:
                t += freeze["dur"]
            return t

        # Beat alignment: snap the segment END to the nearest beat (trimming only)
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

        # Beat context for effects: period + phase (the first beat after the
        # segment's position on the timeline, relative to the segment start)
        period = 60.0 / beats["bpm"] if beats.get("bpm") else None
        beat_ctx = None
        if period:
            next_beat = next(
                (t for t in beats["beat_times"] if t >= timeline_pos), None
            )
            phase = (next_beat - timeline_pos) % period if next_beat else 0.0
            beat_ctx = {"period": period, "phase": phase}

        # letterbox on climaxes: slow-mo segments and clutches
        letterbox = (
            fx_on
            and fx.get("letterbox_on_slowmo", False)
            and choose(
                "letterbox",
                bool(decision.slowmo_segments)
                or getattr(cand, "kind", None) == "clutch",
            )
        )

        # Kill punch: deep crosshair zoom on the clip's LAST kill; dutch tilt
        # only on the film's climax kill (highest order). Times are mapped
        # from source to output time, and the kill must sit safely inside
        # the beat-trimmed segment.
        kill_fx = None
        if kill_zoom_active and kills:
            t_out = _out_time(kills[-1].offset_s)
            # with a freeze, both the freeze and the snap-out must fit before the segment ends
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
                    # hold the zoom through the freeze, snap out when the image wakes up
                    kill_fx["hold_until"] = t_out + freeze["dur"] + 0.10

        # punch times in output time, safely inside the trimmed segment
        hs_punches: list[dict] = []
        strength = max(0.0, float(fx.get("headshot_punch_strength", 1.0)))
        dz = min(0.22, 0.15 * strength * k)
        if hs_marks and dz > 0.01:
            for t in hs_marks:
                t_out = _out_time(t)
                if 0.5 < t_out < target_duration - 0.25:
                    hs_punches.append({"t": t_out, "dz": dz})

        # Kill counter on multikills/aces (>= 3 kills). The last tick shows
        # "4K"/"ACE"; if it lands on a freeze frame it stays as a full stop.
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

        # Kill tags: the style details of each kill, in output time
        tags = None
        if fx_on and fx.get("kill_tags", False) and kills:
            tags = []
            for km in kills:
                label = kill_tag(km)
                t_out = _out_time(km.offset_s)
                if label and 0.2 < t_out < target_duration - 0.4:
                    tags.append((t_out, label))
            tags = tags or None

        # context line from Highlight fields (never from the Director);
        # missing fields are skipped, never an empty "None"
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

        # "Big kills" = the punch selection (headshots + last kill). The same
        # times are used for the chroma split in the image and impact SFX in the mix.
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
            src_fps=probe_fps(clip_path),  # high-fps source → real slow-mo
            frame=frame, tags=tags,
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
        # Actual sync error: how far the cut lands from the beat after
        # encoding (frame granularity ~17 ms at 60 fps). The target is ±100 ms.
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
                # impact SFX is mixed at these times (relative to the
                # segment start) during assembly
                "sfx_impact_times_s": [round(t, 3) for t in impact_times],
            }
        )
        timeline_pos += actual
        segment_files.append(out_path)

        # rewind + slow-mo replay right after the climax segment
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
                frame=frame,
            )
            # impact SFX when the kill lands in the replay (after the rewind)
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
