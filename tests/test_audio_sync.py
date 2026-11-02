"""Tests that the game audio stays in sync with the picture through
concat_timeline + assemble.

Each mock part has a white flash and a beep at the same moment (1.0 s in).
In the finished film, every beep must still land on its flash. The stream
copy that joins the parts keeps each part's AAC priming and padding, which
used to push the game audio ~27 ms later per join.

Run: python tests/test_audio_sync.py
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.assemble import assemble  # noqa: E402
from lib.edit_engine import ENCODE_ARGS  # noqa: E402
from lib.models import EditDecision, EditPlan  # noqa: E402

FAILS: list[str] = []
# different lengths, so the parts don't all end on an AAC frame boundary
PART_SECONDS = (2.0, 1.7, 2.3, 2.0, 1.9, 2.1, 2.0, 2.2)


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_part(path: Path, seconds: float) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=black:s=1920x1080:r=60:d={seconds}",
         "-f", "lavfi", "-i", f"sine=f=1000:d={seconds}:r=48000",
         "-vf", "drawbox=c=white:t=fill:enable='between(t,0.99,1.005)'",
         "-af", "volume=0:enable='not(between(t,1.0,1.03))',volume=4",
         *ENCODE_ARGS, str(path)],
        check=True,
    )


def flashes(path: Path) -> list[float]:
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-vf",
         "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-f", "null", "-"],
        capture_output=True, text=True,
    ).stdout
    times: list[float] = []
    pts = 0.0
    for line in out.splitlines():
        if "pts_time:" in line:
            pts = float(line.split("pts_time:")[1].split()[0])
        elif "YAVG" in line and float(line.split("=")[1]) > 100:
            if not times or pts - times[-1] > 0.5:
                times.append(pts)
    return times


def beeps(path: Path) -> list[float]:
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a", "-af", "pan=mono|c0=c0",
         "-ar", "48000", "-f", "f32le", "-"],
        capture_output=True, check=True,
    ).stdout
    s = struct.unpack(f"<{len(raw) // 4}f", raw)
    times: list[float] = []
    # step 7, not 48: 48 samples is one period of the 1 kHz beep, so every
    # probe would hit the same phase (and could miss the beep entirely)
    for i in range(0, len(s), 7):
        if abs(s[i]) > 0.05 and (not times or i / 48000 - times[-1] > 0.5):
            j = max(0, i - 7)
            while j < i and abs(s[j]) <= 0.05:
                j += 1
            times.append(j / 48000)
    return times


def test_sync(tmp: Path) -> None:
    print("game audio vs picture")
    log = tmp / "log.txt"
    parts = []
    for k, secs in enumerate(PART_SECONDS):
        part = tmp / f"part{k}.mp4"
        make_part(part, secs)
        parts.append(part)
    music = tmp / "silence.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "anullsrc=r=48000:cl=stereo", "-t", "30", str(music)], check=True)
    plan = EditPlan(
        demo_path="x.dem", music_track="silence.wav", intro_text="", outro_text="",
        decisions=[EditDecision(clip_id="a", order=1, slowmo_segments=[], overlay_text=None,
                                transition_in="cut", reasoning="test")],
        dropped_clip_ids=[],
    )
    cfg = {
        "effects": {"intro_hook": False},
        "editing": {"intro_seconds": 0.5, "outro_seconds": 0.5,
                    "game_audio_volume": 1.0, "sfx": False, "sidechain_duck": False},
    }
    (tmp / "04-segments").mkdir()
    # the parts go in as the cold open, so they're joined like any other part
    final = assemble(plan, [], music, tmp, cfg, log, hook_files=parts, cut_log=[])

    f, b = flashes(final), beeps(final)
    check(f"all {len(PART_SECONDS)} flashes and beeps found",
          len(f) == len(b) == len(PART_SECONDS), f"{len(f)} flashes, {len(b)} beeps")
    late = [round((bb - ff) * 1000) for ff, bb in zip(f, b)]
    check("every beep within 15 ms of its flash", all(abs(ms) <= 15 for ms in late), f"{late} ms")
    check("no drift from the first part to the last", late and max(late) - min(late) <= 5,
          f"{late} ms")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        test_sync(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All audio sync tests OK")
