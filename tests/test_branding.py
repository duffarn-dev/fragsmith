"""Tests for [branding]: card background images and the logo on the film.

Covers: finding the files (missing ones become warnings), the logo position
and size, cards with a 4:3 and a portrait image (they must fill the frame,
darkened), and the logo in the concat step (right corner, yuv420p, audio kept).

Run: python tests/test_branding.py
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

from lib.assemble import _stats_card, _text_card, concat_timeline  # noqa: E402
from lib.branding import branding_files, logo_overlay  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def probe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height,pix_fmt,r_frame_rate:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return {
        "w": video["width"], "h": video["height"], "pix_fmt": video["pix_fmt"],
        "fps": video["r_frame_rate"], "duration": float(data["format"]["duration"]),
        "audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def pixel(path: Path, t: float, x: int, y: int) -> tuple[int, int, int]:
    """RGB of a 4x4 block averaged to one pixel."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(path), "-frames:v", "1",
         "-vf", f"crop=4:4:{x}:{y},scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True,
    ).stdout
    return raw[0], raw[1], raw[2]


def test_files(tmp: Path) -> None:
    print("finding the branding files")
    files, warnings = branding_files({})
    check("nothing set: no files, no warnings", not any(files.values()) and not warnings)
    logo = tmp / "logo.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=white:s=100x100", "-frames:v", "1", str(logo))
    files, warnings = branding_files({"branding": {"logo": str(logo), "intro_image": "nope.png"}})
    check("an existing file is found", files["logo"] == logo)
    check("a missing file is a warning, not a stop",
          files["intro_image"] is None and len(warnings) == 1 and "intro_image" in warnings[0],
          str(warnings))
    files, _ = branding_files({"branding": {"intro_image": "assets/luts/fragsmith-teal-orange.cube"}})
    check("a relative path is anchored in the project folder",
          files["intro_image"] == ROOT / "assets" / "luts" / "fragsmith-teal-orange.cube")
    _, warnings = branding_files({"branding": {"logo": str(logo), "logo_position": "middle"}})
    check("an unknown position is a warning", any("logo_position" in w for w in warnings))


def test_overlay() -> None:
    print("logo position and size")
    br = logo_overlay({"logo_position": "bottom-right"}, 1920)
    tl = logo_overlay({"logo_position": "top-left"}, 1920)
    check("bottom-right", "x=W-w-48:y=H-h-48" in br, br)
    check("top-left", "x=48:y=48" in tl, tl)
    check("unknown position falls back to bottom-right",
          "x=W-w-48:y=H-h-48" in logo_overlay({"logo_position": "middle"}, 1920))
    check("width is a share of the frame (12% of 1920 = 230)", "scale=230:-1" in br, br)
    check("size is clamped", "scale=960:-1" in logo_overlay({"logo_size": 5}, 1920))
    check("a 9:16 frame gets a smaller logo and margin",
          "scale=130:-1" in logo_overlay({}, 1080) and "W-w-27" in logo_overlay({}, 1080))
    check("ends in yuv420p (the purple-film trap)", br.endswith("format=yuv420p[v]"))


def test_cards(tmp: Path) -> None:
    print("cards with your own image")
    # bright colors: pure red/blue have so little luma that "darkened" and
    # "black" are hard to tell apart
    wide = tmp / "wide.png"
    tall = tmp / "tall.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=0xffff00:s=800x600", "-frames:v", "1", str(wide))
    ffmpeg("-f", "lavfi", "-i", "color=c=0xc8c8c8:s=600x1000", "-frames:v", "1", str(tall))
    log = tmp / "ffmpeg.log"

    card = tmp / "card_wide.mp4"
    _text_card("HELLO", 1.5, card, log, "test card", wide)
    info = probe(card)
    check("1920x1080, yuv420p, 60 fps",
          (info["w"], info["h"], info["pix_fmt"], info["fps"]) == (1920, 1080, "yuv420p", "60/1"),
          str(info))
    check("the right length, with audio", abs(info["duration"] - 1.5) < 0.1 and info["audio"])
    r, g, b = pixel(card, 0.75, 4, 4)
    # drawbox darkens in YUV, which also pulls the color a bit toward gray
    check("a 4:3 image fills the corners", r > 100 and g > 100 and b < r - 30, f"rgb {r},{g},{b}")
    check("and is darkened so the text reads", r < 215 and g < 215, f"rgb {r},{g},{b}")

    card = tmp / "card_tall.mp4"
    _text_card("HELLO", 1.5, card, log, "test card", tall)
    corners = [pixel(card, 0.75, 4, 4), pixel(card, 0.75, 1912, 1072)]
    check("a portrait image fills the corners too (gray 200 -> ~130)",
          all(100 < c[1] < 160 for c in corners), str(corners))

    card = tmp / "card_black.mp4"
    _text_card("HELLO", 1.5, card, log, "test card")
    check("no image: still black", max(pixel(card, 0.75, 4, 4)) < 20)

    card = tmp / "stats.mp4"
    stats = {"map": "de_test", "kills": 20, "kd": 1.25, "hs_pct": 55, "best_clutch": 2}
    _stats_card("GG", "player", stats, 2.0, card, log, wide)
    check("the stats card takes an image as well", pixel(card, 1.0, 4, 4)[0] > 100)


def test_logo_in_concat(tmp: Path) -> None:
    print("the logo on the whole film")
    parts = []
    for i, src in enumerate(("testsrc2", "smptebars")):
        part = tmp / f"part{i}.mp4"
        ffmpeg("-f", "lavfi", "-i", f"{src}=s=640x360:r=60:d=1",
               "-f", "lavfi", "-i", "sine=f=440:d=1:r=48000",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ac", "2", str(part))
        parts.append(part)
    logo = tmp / "white.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=white:s=100x100,format=rgba", "-frames:v", "1", str(logo))
    log = tmp / "ffmpeg.log"

    work = tmp / "plain"
    work.mkdir()
    plain = concat_timeline(parts, work, log)
    check("without a logo: a plain copy of both parts", abs(probe(plain)["duration"] - 2.0) < 0.1)

    work = tmp / "logo"
    work.mkdir()
    branded = concat_timeline(
        parts, work, log, logo,
        {"logo_size": 0.2, "logo_opacity": 1.0, "logo_position": "bottom-right"}, frame_w=640,
    )
    info = probe(branded)
    check("with a logo: same length, audio kept, yuv420p",
          abs(info["duration"] - 2.0) < 0.1 and info["audio"] and info["pix_fmt"] == "yuv420p",
          str(info))
    # 20% of 640 = 128 px wide, 16 px margin: the logo covers x 496-624, y 216-344
    for t in (0.5, 1.5, 1.95):
        rgb = pixel(branded, t, 556, 276)
        check(f"logo in the bottom-right corner at {t} s", min(rgb) > 225, str(rgb))
    check("not in the top-left corner", min(pixel(branded, 0.5, 40, 40)) < 225)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        test_files(Path(t))
    test_overlay()
    with tempfile.TemporaryDirectory() as t:
        test_cards(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_logo_in_concat(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All branding tests OK")
