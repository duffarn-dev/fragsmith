"""Tests for the velocity cold open ([effects].intro_hook_style = "velocity").

Covers: the slow-mo factor per source frame rate, which kills are picked
(the best, rising, the best last, no moment twice), and the rendered cold
open: quick kills, then the last one in slow-mo, rewound from slow up to
normal speed, and played at normal speed (their lengths, the speed and
direction of each part, sound as long as the picture). The default glimpses
are unchanged.

Run: python tests/test_velocity_intro.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.assemble import (  # noqa: E402
    VELOCITY_FINAL_POST_S,
    VELOCITY_REWIND_SPEEDS,
    VELOCITY_REWIND_TO_S,
    VELOCITY_RUN_POST_S,
    VELOCITY_RUN_PRE_S,
    VELOCITY_RUN_SPEED,
    VELOCITY_SLOW_FROM_S,
    VELOCITY_SLOW_TO_S,
    _velocity_picks,
    build_intro_hook,
    velocity_slow_factor,
)
from lib.ffmpeg_util import probe_duration  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_clip(path: Path, seconds: float, rate: int) -> None:
    """A white bar moving right at 50 px per second of source, on black
    (overlay, not drawbox: drawbox works out its position once)."""
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", f"color=black:s=320x180:r={rate}:d={seconds}",
                    "-f", "lavfi", "-i", f"color=white:s=6x60:r={rate}:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
                    "-filter_complex", "[0:v][1:v]overlay=x='t*50':y=60[v]",
                    "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def km(offset: float, hs: bool = False) -> KillMark:
    return KillMark(tick=int(offset * 64), offset_s=offset, weapon="ak47", headshot=hs,
                    victim_name="bot", victim_steamid="765x")


def hl(cid: str, score: float, kills: list[KillMark]) -> Highlight:
    return Highlight(clip_id=cid, start_tick=0, end_tick=320, player_steamid="765me",
                     kill_count=len(kills), kind="multikill", hype_score=score,
                     duration_seconds=5.0, kills=kills, player_name="Tester", round=5)


def fixture(tmp: Path):
    clips = tmp / "clips"
    clips.mkdir()
    make_clip(clips / "a.mp4", 5.0, 60)
    make_clip(clips / "b.mp4", 5.0, 240)  # a motion blur clip
    cands = {
        "a": hl("a", 5.0, [km(1.5), km(1.9, hs=True), km(4.0)]),
        "b": hl("b", 8.0, [km(2.5, hs=True)]),
    }
    plan = EditPlan(demo_path="d.dem", music_track="m.mp3", intro_text="X", outro_text="GG",
                    decisions=[EditDecision(clip_id=c, order=i + 1, slowmo_segments=[],
                                            overlay_text=None, transition_in="cut",
                                            reasoning="t") for i, c in enumerate(cands)],
                    dropped_clip_ids=[])
    return clips, cands, plan


def bar_x(path: Path, t: float) -> float:
    """Where the white bar is at output time t: its middle, in the source's
    320 px (the glimpse is scaled up to the film's frame)."""
    row = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                          "-frames:v", "1", "-vf", "format=gray,crop=iw:1:0:ih/2",
                          "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True, check=True).stdout
    xs = [i for i, v in enumerate(row) if v > 128]
    return sum(xs) / len(xs) * 320 / len(row) if xs else -1.0


def speed(path: Path, t0: float, t1: float) -> float:
    """How fast the bar moves between t0 and t1, in pixels per output second."""
    return (bar_x(path, t1) - bar_x(path, t0)) / (t1 - t0)


def test_rules() -> None:
    print("the rules")
    check("60 and 120 fps: 0.5x", velocity_slow_factor(60) == 0.5 and velocity_slow_factor(120) == 0.5)
    check("240 and 480 fps: 0.25x, real frames", velocity_slow_factor(240) == 0.25
          and velocity_slow_factor(480) == 0.25)
    check("in between: as slow as the frames allow", abs(velocity_slow_factor(180) - 1 / 3) < 1e-9)
    check("the rewind goes from slow up to normal", list(VELOCITY_REWIND_SPEEDS)
          == sorted(VELOCITY_REWIND_SPEEDS) and VELOCITY_REWIND_SPEEDS[-1] == 1.0)


def test_picks(tmp: Path) -> None:
    print("the kills it picks")
    clips, cands, plan = fixture(tmp)
    picks = _velocity_picks(plan, cands, clips, 3)
    check("three picks", len(picks) == 3, str([(c.stem, o) for _, c, o in picks]))
    check("rising: the best last", [p[0] for p in picks] == sorted(p[0] for p in picks)
          and picks[-1][1].stem == "b")
    a_offsets = sorted(o for _, c, o in picks if c.stem == "a")
    check("no moment twice: kills in one clip a glimpse apart",
          all(y - x >= VELOCITY_REWIND_TO_S + VELOCITY_FINAL_POST_S
              for x, y in zip(a_offsets, a_offsets[1:])), str(a_offsets))
    check("the headshot beats the kill right before it", 1.9 in a_offsets and 1.5 not in a_offsets)


def test_render(tmp: Path) -> None:
    print("the cold open")
    clips, cands, plan = fixture(tmp)
    log = tmp / "log.txt"
    fx = {"enabled": True, "intro_hook": True, "intro_hook_style": "velocity", "intro_hook_kills": 3}
    hooks = build_intro_hook(plan, cands, clips, tmp, {"effects": fx}, log)
    check("one file per kill", len(hooks) == 3, str(len(hooks)))
    quick = (VELOCITY_RUN_PRE_S + VELOCITY_RUN_POST_S) / VELOCITY_RUN_SPEED
    check("the quick kills: short", all(abs(probe_duration(h) - quick) <= 0.05 for h in hooks[:-1]),
          ", ".join(f"{probe_duration(h):.3f}" for h in hooks[:-1]))

    # the last kill is b's, from 240 fps: slow-mo at 0.25x. The bar moves
    # 50 px per source second, so its speed in the film is 50 x the speed
    run = (VELOCITY_RUN_PRE_S - VELOCITY_SLOW_FROM_S) / VELOCITY_RUN_SPEED
    slow = (VELOCITY_SLOW_FROM_S + VELOCITY_SLOW_TO_S) / 0.25
    third = (VELOCITY_SLOW_TO_S + VELOCITY_REWIND_TO_S) / 3
    rew = [third / s for s in VELOCITY_REWIND_SPEEDS]
    final = VELOCITY_REWIND_TO_S + VELOCITY_FINAL_POST_S
    last = hooks[-1]
    want = run + slow + sum(rew) + final
    check("the last kill: in, slow-mo, rewind, normal", abs(probe_duration(last) - want) <= 0.08,
          f"{probe_duration(last):.3f} s, want {want:.3f}")
    edges = [0.0, run, run + slow, run + slow + rew[0], run + slow + rew[0] + rew[1],
             run + slow + sum(rew), want]
    parts = [("in at 1.5x", 50 * VELOCITY_RUN_SPEED), ("slow-mo at 0.25x", 50 * 0.25)] + [
        (f"rewind at {s}x", -50 * s) for s in VELOCITY_REWIND_SPEEDS] + [("then normal speed", 50.0)]
    for (name, expect), a, b in zip(parts, edges, edges[1:]):
        pad = min(0.05, (b - a) / 5)
        got = speed(last, a + pad, b - pad)
        check(name, abs(got - expect) <= max(4.0, abs(expect) * 0.15), f"{got:.0f} px/s, want {expect:.0f}")
    a_dur = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                            "stream=duration", "-of", "csv=p=0", str(last)],
                           capture_output=True, text=True).stdout.strip()
    check("the sound is as long as the picture", abs(float(a_dur) - probe_duration(last)) <= 0.08,
          f"audio {a_dur} s")
    (tmp / "plain").mkdir()
    plain = build_intro_hook(plan, cands, clips, tmp / "plain",
                             {"effects": {"enabled": True, "intro_hook": True}}, log)
    check("the default is unchanged: one 0.25 s glimpse per clip, up to 3",
          len(plain) == 2 and all(abs(probe_duration(p) - 0.25) <= 0.05 for p in plain),
          f"{len(plain)} glimpses")


if __name__ == "__main__":
    test_rules()
    with tempfile.TemporaryDirectory() as t:
        test_picks(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_render(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All velocity intro tests OK")
