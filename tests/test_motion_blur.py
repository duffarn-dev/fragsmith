"""Tests for motion blur from a high-fps render (edit_engine, render_clips).

Covers: how many frames are blended at each speed, which clips get blur
(off/climax/all and the Director's overrides), the frame rate each clip is
rendered at, the render-time estimate, the render job, the filter graph,
and that a 480 fps source really comes out blended (and a 60 fps one not).

Run: python tests/test_motion_blur.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.edit_engine import (  # noqa: E402
    _build_filter,
    blur_frames,
    clip_framerates,
    raw_mb_per_min,
    render_cost,
    wants_motion_blur,
)
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402
from lib.render_clips import build_render_job  # noqa: E402

FAILS: list[str] = []
PLAIN_FX = {"grade": False, "vignette": False, "grain": 0.0, "beat_zoom": False,
            "flash_transitions": False, "slowmo_punch": False}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def decision(cid: str, order: int, **kw) -> EditDecision:
    return EditDecision(clip_id=cid, order=order, slowmo_segments=kw.pop("slowmo", []),
                        overlay_text=None, transition_in="cut", reasoning="test", **kw)


def cand(cid: str, kills: int = 2) -> Highlight:
    marks = [KillMark(tick=64 * (2 + 2 * i), offset_s=2.0 + 2 * i, weapon="ak47",
                      headshot=False, victim_name=f"v{i}", victim_steamid="765x")
             for i in range(kills)]
    return Highlight(clip_id=cid, start_tick=0, end_tick=640, player_steamid="765me",
                     kill_count=kills, kind="multikill", hype_score=5.0, round=1,
                     duration_seconds=10.0, kills=marks)


def cfg(mode: str = "climax", **render) -> dict:
    return {"video": {"framerate": 60},
            "render": {"highfps_framerate": 120, "motion_blur": mode,
                       "motion_blur_framerate": 480, **render},
            "effects": {"enabled": True, "instant_replay": False}}


def test_blend_count() -> None:
    print("frames blended per output frame")
    check("480 fps, 180°, normal speed: 4", blur_frames(480, 180, 1.0) == 4)
    check("half speed: 2", blur_frames(480, 180, 0.5) == 2)
    check("0.25x slow-mo: 1, sharp", blur_frames(480, 180, 0.25) == 1)
    check("a 360° shutter: 8", blur_frames(480, 360, 1.0) == 8)
    check("a 120 fps clip: 1 (no blur from too few frames)", blur_frames(120, 180, 1.0) == 1)
    check("a 60 fps clip: 1", blur_frames(60, 180, 1.0) == 1)


def test_which_clips() -> None:
    print("which clips get blur")
    plan = EditPlan(demo_path="x.dem", music_track="m.mp3", intro_text="T", outro_text="GG",
                    dropped_clip_ids=[],
                    decisions=[decision("a", 1), decision("b", 2), decision("c", 3)])
    a, b, c = sorted(plan.decisions, key=lambda d: d.order)
    check("off: none", not any(wants_motion_blur(d, plan, cfg("off")) for d in plan.decisions))
    check("climax: only the last clip",
          [wants_motion_blur(d, plan, cfg("climax")) for d in (a, b, c)] == [False, False, True])
    check("all: every clip", all(wants_motion_blur(d, plan, cfg("all")) for d in plan.decisions))
    b.is_climax = True
    check("climax follows the Director's is_climax",
          [wants_motion_blur(d, plan, cfg("climax")) for d in (a, b, c)] == [False, True, False])
    a.effects_on = ["motion_blur"]
    b.effects_off = ["motion_blur"]
    check("effects_on adds it, effects_off removes it",
          [wants_motion_blur(d, plan, cfg("climax")) for d in (a, b, c)] == [True, False, False])
    check("off is the master switch, even with effects_on",
          not wants_motion_blur(a, plan, cfg("off")))
    no_fx = {**cfg("all"), "effects": {"enabled": False}}
    check("[effects].enabled = false: none", not wants_motion_blur(c, plan, no_fx))
    check("an unknown mode counts as off", not wants_motion_blur(c, plan, cfg("sometimes")))


def test_framerates() -> None:
    print("the frame rate each clip is rendered at")
    plan = EditPlan(demo_path="x.dem", music_track="m.mp3", intro_text="T", outro_text="GG",
                    dropped_clip_ids=[],
                    decisions=[decision("a", 1), decision("slow", 2, slowmo=[
                        {"start_s": 2.0, "end_s": 3.0, "factor": 0.4}]), decision("climax", 3)])
    by_id = {d.clip_id: cand(d.clip_id) for d in plan.decisions}
    fps = clip_framerates(plan, by_id, cfg("climax"))
    check("plain clip at 60, slow-mo at 120, blur at 480",
          (fps["a"], fps["slow"], fps["climax"]) == (60, 120, 480), str(fps))
    fps_off = clip_framerates(plan, by_id, cfg("off"))
    check("blur off: the climax back at 60", fps_off["climax"] == 60, str(fps_off))
    fps_low = clip_framerates(plan, by_id, cfg("climax", motion_blur_framerate=60))
    check("a blur frame rate at or below the base: no blur pass", fps_low["climax"] == 60)

    replay_cfg = cfg("climax")
    replay_cfg["effects"]["instant_replay"] = True
    by_id["climax"] = cand("climax", kills=3)
    fps_r = clip_framerates(plan, by_id, replay_cfg)
    check("the replay's victim POV shares the blur clip's rate",
          fps_r.get("climax") == 480 and fps_r.get("climax_alt") == 480, str(fps_r))

    job = build_render_job("run", plan, by_id, "765me", "me", replay_cfg)
    rates = {c["clip_id"]: c.get("framerate") for c in job["clips"]}
    check("the render job carries them (base clips without a rate)",
          rates == {"a": None, "slow": 120, "climax": 480}, str(rates))


def test_cost() -> None:
    print("render time estimate")
    check("measured: 60 fps 1.6x, 120 fps 3.3x, 480 fps 11x",
          (render_cost(60), render_cost(120), render_cost(480)) == (1.6, 3.3, 11.0))
    check("240 fps scales from 120: 6.6x", abs(render_cost(240) - 6.6) < 1e-9)
    check("below 60 scales down from 60", abs(render_cost(30) - 0.8) < 1e-9)
    check("disk: 480 fps ~4x a 60 fps clip", 4.0 <= raw_mb_per_min(480) / raw_mb_per_min(60) <= 4.5)


def test_graph() -> None:
    print("the filter graph")
    parts = [(0.0, 2.0, 1.0), (2.0, 3.0, 0.25), (3.0, 4.0, 0.5)]
    g, _, _ = _build_filter(parts, decision("a", 1), PLAIN_FX, None, src_fps=480.0,
                            blur_shutter=180.0)
    check("normal speed blends 4", "tmix=frames=4" in g)
    check("half speed blends 2", "tmix=frames=2" in g)
    check("0.25x isn't blended", g.count("tmix") == 2)
    check("the blend comes before the frame rate is cut to 60",
          g.index("tmix=frames=4") < g.index("fps=60"))
    sixty, _, _ = _build_filter(parts, decision("a", 1), PLAIN_FX, None, src_fps=60.0,
                                blur_shutter=180.0)
    check("a 60 fps source isn't blended", "tmix" not in sixty)
    off, _, _ = _build_filter(parts, decision("a", 1), PLAIN_FX, None, src_fps=480.0)
    check("no shutter: no blend", "tmix" not in off)


def test_picture(tmp: Path) -> None:
    print("the blend in the picture")
    src = tmp / "fast.mp4"
    # a 480 fps source where a bar moves 4 px per frame: blended, its edges smear
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-filter_complex",
                    "color=c=black:s=640x360:r=480:d=1[bg];"
                    "color=c=white:s=40x360:r=480:d=1[bar];"
                    "[bg][bar]overlay=x='mod(t*1920,600)':y=0[v];sine=d=1[a]",
                    "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                    "-crf", "1", "-c:a", "aac", "-shortest", str(src)], check=True)

    def render(name: str, shutter: float | None) -> bytes:
        g, v, a = _build_filter([(0.0, 1.0, 1.0)], decision("a", 1), PLAIN_FX, None,
                                src_fps=480.0, blur_shutter=shutter)
        out = tmp / name
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", g,
                        "-map", v, "-map", a, "-t", "1", "-c:v", "libx264", "-preset",
                        "ultrafast", "-crf", "1", str(out)], check=True)
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(out), "-frames:v", "1",
             "-vf", "scale=640:360,crop=640:1:0:180", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, check=True).stdout

    def mid_grays(row: bytes) -> int:
        return sum(1 for v in row if 40 < v < 215)

    sharp, blurred = render("sharp.mp4", None), render("blur.mp4", 180.0)
    check("without blur, the bar's edges are hard", mid_grays(sharp) <= 12, str(mid_grays(sharp)))
    check("with blur, they're smeared", mid_grays(blurred) >= 2 * mid_grays(sharp) + 8,
          f"{mid_grays(blurred)} vs {mid_grays(sharp)} gray pixels across the row")


if __name__ == "__main__":
    test_blend_count()
    test_which_clips()
    test_framerates()
    test_cost()
    test_graph()
    with tempfile.TemporaryDirectory() as t:
        test_picture(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All motion blur tests OK")
