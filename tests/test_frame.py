"""Tests for the output frame (Frame in lib/edit_engine.py).

Landscape must stay exactly as before the frame refactor; a 9:16 frame must
give portrait segments, cards and cold-open glimpses with the same timing.

Run: python tests/test_frame.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.assemble import _text_card, build_intro_hook  # noqa: E402
from lib.edit_engine import LANDSCAPE, PORTRAIT, _max_tilt_degrees, build_segments  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def size_and_duration(path: Path) -> tuple[int, int, float]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    s = data["streams"][0]
    return s["width"], s["height"], float(data["format"]["duration"])


def test_frame() -> None:
    print("the frame")
    check("landscape normalizes exactly as before the refactor",
          LANDSCAPE.normalize() == "scale=1920:1080:force_original_aspect_ratio=decrease,"
                                   "pad=1920:1080:(ow-iw)/2:(oh-ih)/2")
    check("portrait fills the frame and crops the center",
          PORTRAIT.normalize() == "scale=1080:1920:force_original_aspect_ratio=increase,"
                                  "crop=1080:1920")
    check("the tilt limit is the same for 16:9 and 9:16",
          abs(_max_tilt_degrees(1.7, LANDSCAPE) - _max_tilt_degrees(1.7, PORTRAIT)) < 1e-9)


def make_clip(path: Path, seconds: float) -> None:
    # 720p30 on purpose (tests/README.md): the engine has to normalize by itself
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(path)],
        check=True,
    )


def test_segments(tmp: Path) -> None:
    print("segments in both frames")
    kills = [KillMark(tick=int(t * 64), offset_s=t, weapon="ak47", headshot=True,
                      victim_name="bot", victim_steamid="765x") for t in (1.5, 3.0, 4.5)]
    cand = Highlight(clip_id="x", start_tick=0, end_tick=384, player_steamid="765me",
                     kill_count=3, kind="multikill", hype_score=7.0, duration_seconds=6.0,
                     kills=kills, player_name="Tester")
    plan = EditPlan(
        demo_path="mock.dem", music_track="m.mp3", intro_text="X", outro_text="GG",
        decisions=[EditDecision(clip_id="x", order=1, slowmo_segments=[], overlay_text="3K",
                                transition_in="cut", reasoning="test", is_climax=True)],
        dropped_clip_ids=[],
    )
    # the effects that draw or zoom: kill zoom + tilt, punch, counter, lower third, replay
    fx = {"enabled": True, "intensity": 1.0, "kill_zoom": True, "kill_tilt": True,
          "headshot_punch": True, "kill_counter": True, "lowerthird": True,
          "animated_overlays": True, "instant_replay": True, "smooth_slowmo": False,
          "grain": 0.0, "lut": ""}
    cfg = {"effects": fx, "editing": {"beat_tolerance_seconds": 0.0, "intro_seconds": 0.0}}
    beats = {"bpm": 120.0, "beat_times": [], "drops": []}
    clips = tmp / "clips"
    clips.mkdir()
    make_clip(clips / "x.mp4", 6.0)
    log = tmp / "ffmpeg.log"

    results = {}
    for name, frame in (("landscape", LANDSCAPE), ("portrait", PORTRAIT)):
        segs, cut_log, _ = build_segments(
            plan, {"x": cand}, clips, tmp / name, beats, cfg, log,
            context={"map": "de_test"}, timeline_start=0.0, frame=frame,
        )
        results[name] = [size_and_duration(s) for s in segs]
    land, port = results["landscape"], results["portrait"]
    check("landscape segments are 1920x1080", all(r[:2] == (1920, 1080) for r in land), str(land))
    check("portrait segments are 1080x1920 (clip + replay)",
          len(port) == 2 and all(r[:2] == (1080, 1920) for r in port), str(port))
    check("the frame doesn't change the timing",
          all(abs(a[2] - b[2]) < 0.02 for a, b in zip(land, port)),
          f"{[r[2] for r in land]} vs {[r[2] for r in port]}")
    cmds = [l for l in log.read_text(encoding="utf-8").splitlines() if "cmd:" in l]
    portrait_cmds = " ".join(l for l in cmds if "portrait" in l)
    landscape_cmds = " ".join(l for l in cmds if "landscape" in l)
    check("9:16 keeps the lower third above the captions band (78%)",
          "x=70:y=1498" in portrait_cmds and "x=70:y=1010" in landscape_cmds)
    check("9:16 puts REPLAY below the app bar",
          "y=230:alpha" in portrait_cmds and "y=70:alpha" in landscape_cmds)

    card = tmp / "card.mp4"
    _text_card("TITLE", 1.0, card, log, "portrait card", frame=PORTRAIT)
    check("portrait card", size_and_duration(card)[:2] == (1080, 1920))
    hooks = build_intro_hook(plan, {"x": cand}, clips, tmp, {"effects": {"intro_hook": True}},
                             log, frame=PORTRAIT)
    check("portrait cold-open glimpse",
          hooks and size_and_duration(hooks[0])[:2] == (1080, 1920))


if __name__ == "__main__":
    test_frame()
    with tempfile.TemporaryDirectory() as t:
        test_segments(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All frame tests OK")
