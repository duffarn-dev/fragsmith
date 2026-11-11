"""Tests for the screen effects: impact shake and glow.

Covers: the filter graph (a zoompan for the shake only when there are big
kills and it's on, the glow chain before the effects, nothing when off),
and the rendered picture (the shake moves the image on the kill and leaves
it alone before; the glow brightens around a bright patch and leaves the
dark away from it).

Run: python tests/test_screen_fx.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.edit_engine import _build_filter  # noqa: E402
from lib.models import EditDecision  # noqa: E402

FAILS: list[str] = []
# no other look effects, so a difference between two renders is the effect
PLAIN_FX = {"grade": False, "vignette": False, "grain": 0.0, "beat_zoom": False,
            "flash_transitions": False, "slowmo_punch": False}
W, H = 640, 360


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def decision() -> EditDecision:
    return EditDecision(clip_id="a", order=1, slowmo_segments=[], overlay_text=None,
                        transition_in="cut", reasoning="test")


def gray(path: Path, t: float) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def render(tmp: Path, src: Path, name: str, fx: dict, **kw) -> Path:
    from lib.edit_engine import Frame
    graph, vout, aout = _build_filter([(0.0, 2.0, 1.0)], decision(), fx, None,
                                      frame=Frame(W, H), **kw)
    out = tmp / name
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph,
                    "-map", vout, "-map", aout, "-t", "2", "-c:v", "libx264",
                    "-preset", "ultrafast", "-crf", "10", str(out)], check=True)
    return out


def test_graph() -> None:
    print("the filter graph")
    graph, _, _ = _build_filter([(0.0, 2.0, 1.0)], decision(),
                                {**PLAIN_FX, "impact_shake": True}, None, shakes=[1.0])
    check("a shake is a zoompan around the kill",
          "zoompan" in graph and "between(on/60,1.000,1.350)" in graph)
    none, _, _ = _build_filter([(0.0, 2.0, 1.0)], decision(), PLAIN_FX, None)
    check("no shakes, no zoompan", "zoompan" not in none)
    glow, _, _ = _build_filter([(0.0, 2.0, 1.0)], decision(), {**PLAIN_FX, "glow": 0.8}, None)
    check("the glow is a blend before the effects",
          "[vc]split[gb][gs]" in glow and "[vglow]" in glow and "c0_mode=screen" in glow)
    check("the glow leaves the color planes alone",
          "c1_opacity=0" in glow and "c2_opacity=0" in glow)
    check("glow 0 is off", "split[gb]" not in none)
    off, _, _ = _build_filter([(0.0, 2.0, 1.0)], decision(),
                              {**PLAIN_FX, "glow": 0.8, "enabled": False}, None)
    check("effects off: no glow", "split[gb]" not in off)


def test_picture(tmp: Path) -> None:
    print("the picture")
    src = tmp / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s={W}x{H}:r=60:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-c:a", "aac",
                    "-shortest", str(src)], check=True)
    bare = render(tmp, src, "bare.mp4", PLAIN_FX)
    shaken = render(tmp, src, "shake.mp4", {**PLAIN_FX, "impact_shake": True}, shakes=[1.0])
    before = diff(gray(bare, 0.5), gray(shaken, 0.5))
    during = diff(gray(bare, 1.05), gray(shaken, 1.05))
    after = diff(gray(bare, 1.7), gray(shaken, 1.7))
    check("still before the kill", before < 1.0, f"{before:.2f}")
    check("it moves on the kill", during > 3.0, f"{during:.2f}")
    check("still again once it settles", after < 1.0, f"{after:.2f}")

    # a white square on black: the glow lights up the black next to it
    box = tmp / "box.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"color=black:s={W}x{H}:r=60:d=2", "-f", "lavfi", "-i", "sine=d=2",
                    "-vf", "drawbox=x=280:y=140:w=80:h=80:color=white:t=fill",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "10", "-c:a", "aac",
                    "-shortest", str(box)], check=True)
    plain = gray(render(tmp, box, "box_plain.mp4", PLAIN_FX), 1.0)
    glowed = gray(render(tmp, box, "box_glow.mp4", {**PLAIN_FX, "glow": 1.0}), 1.0)

    def at(img: bytes, x: int, y: int) -> int:
        return img[y * W + x]

    near = at(glowed, 372, 180) - at(plain, 372, 180)  # 12 px right of the box
    far = at(glowed, 40, 40) - at(plain, 40, 40)
    check("the black next to a bright patch glows", near > 12, f"+{near}")
    check("the dark far from it stays dark", abs(far) <= 3, f"{far:+d}")


if __name__ == "__main__":
    test_graph()
    with tempfile.TemporaryDirectory() as t:
        test_picture(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All screen effect tests OK")
