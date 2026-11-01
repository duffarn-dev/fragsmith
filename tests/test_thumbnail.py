"""Tests for lib/thumbnail.py: the climax frame as a 1280x720 JPG.

Covers: which clip and moment are picked (the Director's climax, just before
the last kill), the label (overlay text, else ACE / 4K / 1vX), the size and
file limit, the logo, and no thumbnail (but no crash) when the clip is missing.

Run: python tests/test_thumbnail.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402
from lib.styles import apply_style  # noqa: E402
from lib.thumbnail import MAX_BYTES, _font_size, climax_label, make_thumbnail, pick_moment  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def cand(cid: str, kind: str = "multikill", kills: int = 3, clutch: int | None = None,
         last_kill_s: float = 3.0) -> Highlight:
    marks = [KillMark(tick=64 * i, offset_s=last_kill_s - (kills - 1 - i) * 0.5, weapon="ak47",
                      headshot=True, victim_name=f"v{i}", victim_steamid=f"7656119000000010{i}")
             for i in range(kills)]
    return Highlight(clip_id=cid, start_tick=0, end_tick=640, player_steamid="76561190000000001",
                     kill_count=kills, kind=kind, hype_score=5.0, round=1, duration_seconds=4.0,
                     headshots=kills, kills=marks, clutch_vs=clutch)


def decision(cid: str, order: int, overlay: str | None = None, climax: bool = False) -> EditDecision:
    return EditDecision(clip_id=cid, order=order, slowmo_segments=[], overlay_text=overlay,
                        transition_in="cut", reasoning="test", is_climax=climax)


def plan(*decisions: EditDecision) -> EditPlan:
    return EditPlan(demo_path="x.dem", music_track="a.mp3", intro_text="", outro_text="",
                    decisions=list(decisions), dropped_clip_ids=[])


def test_label() -> None:
    print("the label")
    check("the Director's overlay text wins",
          climax_label(decision("a", 1, "INSANE 1V4"), cand("a", "clutch", 4, 4)) == "INSANE 1V4")
    check("ace", climax_label(decision("a", 1), cand("a", "ace", 5)) == "ACE")
    check("clutch", climax_label(decision("a", 1), cand("a", "clutch", 2, 3)) == "1v3")
    check("multikill", climax_label(decision("a", 1), cand("a", "multikill", 4)) == "4K")
    check("a single kill has no label", climax_label(decision("a", 1), cand("a", "flick", 1)) == "")
    check("short labels stay big, long ones shrink to fit",
          _font_size("3K") == 130 and _font_size("THE CLUTCH THAT WON THE HALF") < 70)


def test_moment(tmp: Path) -> None:
    print("picking the moment")
    for cid in ("r03_a", "r19_b"):
        (tmp / f"{cid}.mp4").write_bytes(b"x")
    by_id = {"r03_a": cand("r03_a", last_kill_s=1.0), "r19_b": cand("r19_b", last_kill_s=3.0)}
    clip, t, label = pick_moment(plan(decision("r03_a", 1), decision("r19_b", 2, "3K")), by_id, tmp)
    check("no climax set: the last clip", clip.name == "r19_b.mp4" and label == "3K")
    check("just before the last kill", abs(t - 2.9) < 1e-6, str(t))
    clip, _, _ = pick_moment(plan(decision("r03_a", 1, climax=True), decision("r19_b", 2)), by_id, tmp)
    check("the Director's climax wins", clip.name == "r03_a.mp4")
    check("a missing raw clip gives no thumbnail",
          pick_moment(plan(decision("zz", 1)), {"zz": cand("zz")}, tmp) is None)


def test_make(tmp: Path) -> None:
    print("making the thumbnail")
    # 2 s red, then 2 s blue: the kill at 3.0 s must give a blue frame.
    # Tagged tv range like csdm's clips: the jpeg encoder refused that (2026-11-01).
    clips = tmp / "clips"
    clips.mkdir()
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=1280x720:r=30:d=2",
           "-f", "lavfi", "-i", "color=c=blue:s=1280x720:r=30:d=2",
           "-filter_complex", "[0][1]concat=n=2:v=1:a=0", "-pix_fmt", "yuv420p",
           "-color_range", "tv", str(clips / "r19_b.mp4"))
    # a kill time between two frames, like real ones: with an unlooped logo
    # and shortest=1 the overlay then ended before the first frame, and ffmpeg
    # exited 0 without writing the jpg (2026-11-01)
    by_id = {"r19_b": cand("r19_b", last_kill_s=3.0137)}
    p = plan(decision("r19_b", 1, "3K"))
    log = tmp / "ffmpeg.log"
    cfg = {"effects": {"lut": "", "grain": 0.0}}

    out = make_thumbnail(p, by_id, clips, tmp / "thumbnail.jpg", cfg, log)
    info = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.strip()
    check("1280x720", info == "1280,720", info)
    check("under YouTube's 2 MB", out.stat().st_size < MAX_BYTES, f"{out.stat().st_size} bytes")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "crop=8:8:636:356,scale=1:1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    check("the frame comes from the kill (blue), not the start (red)", raw[2] > raw[0] + 60,
          f"rgb {raw[0]},{raw[1]},{raw[2]}")

    # a logo with real transparency around a white box: that's what made the
    # jpeg encoder refuse the frame (limited-range output) on a real film
    logo = tmp / "logo.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=white:s=200x100,format=rgba",
           "-vf", "geq=r=255:g=255:b=255:a='if(between(X,20,179)*between(Y,20,79),255,0)'",
           "-frames:v", "1", str(logo))
    # with the hype look (wobble, vibrance...) like the real film that failed
    cfg = apply_style({"effects": {"lut": ""}}, "hype")
    cfg["branding"] = {"logo": str(logo), "logo_opacity": 1.0}
    out = make_thumbnail(p, by_id, clips, tmp / "thumb_logo.jpg", cfg, log)
    # 12% of 1280 = 154 px wide, 32 px margin: the white box is around x 1109-1233, y 626-672
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-vf", "crop=8:8:1167:645,scale=1:1",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True).stdout
    check("the logo in the bottom-right corner", min(raw) > 220, f"rgb {raw[0]},{raw[1]},{raw[2]}")

    check("no raw clip: None, no crash",
          make_thumbnail(plan(decision("zz", 1)), {"zz": cand("zz")}, clips, tmp / "n.jpg", cfg, log) is None)


if __name__ == "__main__":
    test_label()
    with tempfile.TemporaryDirectory() as t:
        test_moment(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_make(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All thumbnail tests OK")
