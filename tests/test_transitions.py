"""Tests for the transitions on clip changes (edit_engine).

Covers: which cuts get a transition, that the timeline is the same as with
hard cuts (every clip start and the film length, ±1 frame), that each clip's
picture is where it was, that the drop-sync simulation still predicts the
climax start, and that every kind encodes with sound and picture in step.

Mock clips in 720p/30fps (see tests/README.md); the engine should normalize.
Run: python tests/test_transitions.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.edit_engine import (  # noqa: E402
    TRANSITIONS,
    _simulate_timeline,
    build_segments,
    pick_transitions,
)
from lib.ffmpeg_util import probe_duration  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight  # noqa: E402

FAILS: list[str] = []
FRAME = 1 / 60
TIMELINE_START = 2.5


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_clip(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:d={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def decision(cid: str, order: int, transition_in: str = "cut") -> EditDecision:
    return EditDecision(clip_id=cid, order=order, slowmo_segments=[], overlay_text=None,
                        transition_in=transition_in, reasoning="test")


def cfg_with(transitions: list[str]) -> dict:
    """No look effects and no kills, so the only difference is the transitions."""
    return {
        "effects": {
            "enabled": True, "grade": False, "vignette": False, "grain": 0.0,
            "beat_zoom": False, "flash_transitions": False, "slowmo_punch": False,
            "eased_ramps": False, "smooth_slowmo": False, "lowerthird": False,
            "instant_replay": False, "transitions": transitions,
        },
        "editing": {"beat_tolerance_seconds": 0.75},
    }


def beats_grid(bpm: float = 120.0, track_s: float = 60.0) -> dict:
    period = 60.0 / bpm
    return {"duration_seconds": track_s, "bpm": bpm, "drops": [],
            "beat_times": [round(i * period, 3) for i in range(1, int(track_s / period))]}


def gray(path: Path, t: float) -> bytes:
    """A small gray frame at t (seeked after -i, so it's frame accurate)."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-ss", f"{t:.4f}", "-frames:v", "1",
         "-vf", "scale=320:180", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def stream_durations(path: Path) -> tuple[float, float]:
    out = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "json", str(path)], capture_output=True, text=True).stdout)["streams"]
    by = {s["codec_type"]: float(s["duration"]) for s in out}
    return by.get("video", 0.0), by.get("audio", 0.0)


def test_pick() -> None:
    print("which cuts get a transition")
    ds = [decision("a", 1), decision("b", 2), decision("c", 3), decision("d", 4)]
    on = {"enabled": True, "transitions": ["whip", "zoom"]}
    check("the first clip comes in from the intro, without one",
          "a" not in pick_transitions(ds, on, None))
    check("the style's list takes turns",
          pick_transitions(ds, on, None) == {"b": "whip", "c": "zoom", "d": "whip"})
    check("[] = hard cuts", pick_transitions(ds, {"enabled": True, "transitions": []}, None) == {})
    check("effects off = hard cuts",
          pick_transitions(ds, {"enabled": False, "transitions": ["whip"]}, None) == {})
    check("not out of the replay clip", "c" not in pick_transitions(ds, on, "b"))
    faded = [decision("a", 1), decision("b", 2, "fade")]
    check("not into a fade", pick_transitions(faded, on, None) == {})
    chosen = [decision("a", 1), decision("b", 2, "zoom"), decision("c", 3, "glitch")]
    picks = pick_transitions(chosen, on, None)
    check("the Director's kind wins when the style has it", picks["b"] == "zoom")
    check("a kind the style doesn't have takes the list's turn", picks["c"] == "zoom")
    check("unknown kinds in the config are ignored",
          pick_transitions(ds, {"enabled": True, "transitions": ["spin"]}, None) == {})


def test_timeline(tmp: Path) -> None:
    print("the timeline with transitions")
    clips = tmp / "clips"
    clips.mkdir()
    # c1 is trimmed by the beat snap (its tail has real frames past the cut);
    # c2 ends on a beat (its tail holds the last frame)
    for cid, secs in (("c1", 6.3), ("c2", 6.0), ("c3", 6.0)):
        make_clip(clips / f"{cid}.mp4", secs)
    cands = {
        cid: Highlight(clip_id=cid, start_tick=0, end_tick=384, player_steamid="765me",
                       kill_count=2, kind="multikill", hype_score=3.0, round=i + 1,
                       duration_seconds=6.0, player_name="Tester")
        for i, cid in enumerate(("c1", "c2", "c3"))
    }
    plan = EditPlan(demo_path="mock.dem", music_track="mock.mp3", intro_text="T",
                    outro_text="GG", dropped_clip_ids=[],
                    decisions=[decision("c1", 1), decision("c2", 2), decision("c3", 3)])
    beats = beats_grid()
    log = tmp / "ffmpeg.log"

    def build(name: str, kinds: list[str]):
        return build_segments(plan, cands, clips, tmp / name, beats, cfg_with(kinds),
                              log, timeline_start=TIMELINE_START)

    off_files, off_log, _ = build("off", [])
    on_files, on_log, _ = build("on", ["whip", "zoom"])

    kinds = [c.get("transition_in") for c in on_log]
    check("whip into c2, zoom into c3", kinds == [None, "whip", "zoom"], str(kinds))
    ends_off = [c["timeline_end_s"] for c in off_log]
    ends_on = [c["timeline_end_s"] for c in on_log]
    check("every clip starts and ends where it did (±1 frame)",
          all(abs(a - b) <= FRAME + 1e-3 for a, b in zip(ends_off, ends_on)),
          f"{ends_off} vs {ends_on}")
    total_off = sum(probe_duration(p) for p in off_files)
    total_on = sum(probe_duration(p) for p in on_files)
    check("the film is as long as with hard cuts (±1 frame)",
          abs(total_off - total_on) <= FRAME + 1e-3, f"{total_off:.3f} vs {total_on:.3f} s")
    cut_at = [c["cut_at_s"] for c in on_log]
    want = [0.0, round(TRANSITIONS["whip"] / 120, 3), round(TRANSITIONS["zoom"] / 120, 3)]
    check("each clip starts half a transition into its file", cut_at == want,
          f"{cut_at} vs {want}")
    tails = sorted(p.name for p in (tmp / "on").glob("*_tail.mkv"))
    check("a tail for each clip a transition leaves",
          tails == ["01_c1_tail.mkv", "02_c2_tail.mkv"], str(tails))

    for p in on_files:
        v, a = stream_durations(p)
        check(f"{p.name}: sound as long as the picture (±30 ms)", abs(v - a) <= 0.03,
              f"video {v:.3f} s, audio {a:.3f} s")

    # the picture: each clip's frame at its own second 1 is the same frame
    c1 = diff(gray(off_files[0], 1.0), gray(on_files[0], 1.0))
    check("c1 is unchanged before its transition", c1 < 1.0, f"{c1:.2f}")
    c2 = diff(gray(off_files[1], 1.0), gray(on_files[1], 1.0 + cut_at[1]))
    check("c2's picture is on the same beat", c2 < 1.0, f"{c2:.2f}")
    c2_late = diff(gray(off_files[1], 1.0), gray(on_files[1], 1.0 + cut_at[1] + 2 * FRAME))
    check("(the check sees a 2-frame shift)", c2_late > 2 * c2 + 0.5,
          f"{c2_late:.2f} vs {c2:.2f}")
    mid = diff(gray(off_files[1], 0.0), gray(on_files[1], cut_at[1]))
    check("the transition is visible on the cut", mid > 5.0, f"{mid:.2f}")

    # drop sync predicts from the same timing: the climax (c3) starts where
    # the simulation says, and the segments end where it says
    sim_start, sim_end = _simulate_timeline(plan, cands, clips, beats, cfg_with(["whip", "zoom"]),
                                            TIMELINE_START, {})
    c3 = on_log[-1]
    real_start = c3["timeline_end_s"] - c3["segment_duration_s"]
    check("the simulated climax start holds (±1 frame)",
          sim_start is not None and abs(sim_start - real_start) <= FRAME + 1e-3,
          f"{sim_start} vs {real_start:.3f}")
    check("the simulated end holds (±1 frame)", abs(sim_end - ends_on[-1]) <= FRAME + 1e-3,
          f"{sim_end:.3f} vs {ends_on[-1]:.3f}")

    glitch_files, glitch_log, _ = build("glitch", ["glitch"])
    check("glitch encodes and keeps the timeline",
          [c.get("transition_in") for c in glitch_log] == [None, "glitch", "glitch"]
          and all(abs(a - c["timeline_end_s"]) <= FRAME + 1e-3
                  for a, c in zip(ends_off, glitch_log)))


if __name__ == "__main__":
    test_pick()
    with tempfile.TemporaryDirectory() as t:
        test_timeline(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All transition tests OK")
