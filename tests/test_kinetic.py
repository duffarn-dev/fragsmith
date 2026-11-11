"""Tests for lib/kinetic.py and the kinetic text path in the editing engine.

Covers: the slam curve, the looks ([effects].text_color and text_animation:
pop, glitch and type next to the slam), the rendered clip (rgba, 60 fps, length, the
overshoot is bigger than the settled text, the fade-out, a held last frame,
the amber fill of the kill tags), the cache, the filter graph (movie sources
instead of drawtext for the overlay text, the counter and the kill tags,
placed at their start, under the letterbox), and that the text only changes
the picture inside its window.

Run: python tests/test_kinetic.py
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

from lib.edit_engine import _build_filter  # noqa: E402
from lib.kinetic import (  # noqa: E402
    AMBER,
    COLORS,
    WHITE,
    kinetic_clip,
    pop_scale_at,
    scale_at,
    text_animation,
    text_color,
)
from lib.models import EditDecision  # noqa: E402

FAILS: list[str] = []
# no look effects, so the only difference between two renders is the text
PLAIN_FX = {"grade": False, "vignette": False, "grain": 0.0, "beat_zoom": False,
            "flash_transitions": False, "slowmo_punch": False}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def decision(text: str | None) -> EditDecision:
    return EditDecision(clip_id="a", order=1, slowmo_segments=[], overlay_text=text,
                        transition_in="cut", reasoning="test")


def alpha_of(clip: Path, n: int) -> tuple[int, int]:
    """(max alpha, width of the visible area) of frame n."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip), "-vf", f"select='eq(n\\,{n})'",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    w = int(json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "json", str(clip)],
        capture_output=True, text=True).stdout)["streams"][0]["width"])
    alphas = raw[3::4]
    cols = [i % w for i, a in enumerate(alphas) if a > 40]
    return max(alphas), (max(cols) - min(cols) if cols else 0)


def letter_color(clip: Path, n: int) -> tuple[int, int, int]:
    """The average color of frame n's opaque, bright pixels: the letters,
    without the black stroke or the glow."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip), "-vf", f"select='eq(n\\,{n})'",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout
    px = [raw[i:i + 3] for i in range(0, len(raw), 4)
          if raw[i + 3] == 255 and raw[i] > 200]
    if not px:
        return (0, 0, 0)
    return tuple(round(sum(p[c] for p in px) / len(px)) for c in range(3))


def gray_frame(path: Path, t: float) -> bytes:
    """The area around the overlay text (centered, from 0.70 of the height)."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", "crop=480:180:720:756", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, check=True).stdout


def diff(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a))


def test_curve() -> None:
    print("the slam curve")
    check("starts at 1.9x", abs(scale_at(0) - 1.9) < 1e-9)
    check("overshoots to 0.94x at 0.12 s", abs(scale_at(0.12) - 0.94) < 1e-6)
    check("settles at 1.0x", scale_at(0.22) == 1.0 and scale_at(2.0) == 1.0)
    steps = [scale_at(i / 100) for i in range(13)]
    check("shrinks the whole slam", all(a > b for a, b in zip(steps, steps[1:])))


def test_clip(tmp: Path) -> None:
    print("the rendered clip")
    log = tmp / "log.txt"
    clip, cw, ch = kinetic_clip("3K", 110, 1.0, tmp / "k", log)
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-show_entries",
         "stream=pix_fmt,r_frame_rate,nb_read_frames,width,height", "-of", "json", str(clip)],
        capture_output=True, text=True).stdout)["streams"][0]
    check("rgba at 60 fps", info["pix_fmt"] == "rgba" and info["r_frame_rate"] == "60/1",
          f"{info['pix_fmt']} {info['r_frame_rate']}")
    check("1.0 s = 60 frames", int(info["nb_read_frames"]) == 60, info["nb_read_frames"])
    check("the canvas size is what the engine places", (info["width"], info["height"]) == (cw, ch))
    _, slam_w = alpha_of(clip, 0)
    settled_a, settled_w = alpha_of(clip, 30)
    check("the first frame is 1.5x the settled text or more", slam_w > settled_w * 1.5,
          f"{slam_w} vs {settled_w} px")
    check("settled text is opaque", settled_a == 255)
    last_a, _ = alpha_of(clip, 59)
    check("it fades out at the end", last_a < 60, f"alpha {last_a}")
    held, _, _ = kinetic_clip("3K", 110, 1.0, tmp / "k", log, fade_out=False)
    check("fade_out=False keeps the last frame", alpha_of(held, 59)[0] == 255 and held != clip)
    mtime = clip.stat().st_mtime_ns
    again, _, _ = kinetic_clip("3K", 110, 1.0, tmp / "k", log)
    check("cached: the same clip, not redrawn", again == clip and clip.stat().st_mtime_ns == mtime)
    check("the frames are cleaned up", not any(p.is_dir() for p in (tmp / "k").iterdir()))
    white, _, _ = kinetic_clip("3K", 110, 1.0, tmp / "k", log, fill=WHITE)
    check("white is the default and keeps its cache key", white == clip)
    # antialiased edges pull the average a few steps off the fill
    def near(color: tuple, target: tuple) -> bool:
        return all(abs(a - b) <= 6 for a, b in zip(color, target))

    color = letter_color(clip, 30)
    check("the default letters are white", near(color, WHITE), str(color))
    amber, _, _ = kinetic_clip("NOSCOPE", 46, 1.1, tmp / "k", log, fill=AMBER)
    color = letter_color(amber, 30)
    check("a kill tag's letters are amber", near(color, AMBER), str(color))
    again_amber, _, _ = kinetic_clip("NOSCOPE", 46, 1.1, tmp / "k", log)
    check("the fill is part of the cache key", again_amber != amber)


def frame_rgba(clip: Path, n: int) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(clip), "-vf", f"select='eq(n\\,{n})'",
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
        capture_output=True, check=True).stdout


def glow_tint(clip: Path, n: int) -> tuple[int, int, int]:
    """The average color of frame n's half-transparent pixels: the glow."""
    raw = frame_rgba(clip, n)
    px = [raw[i:i + 3] for i in range(0, len(raw), 4) if 40 < raw[i + 3] < 200]
    return tuple(round(sum(p[c] for p in px) / max(1, len(px))) for c in range(3))


def red_fringe(clip: Path, n: int) -> int:
    """How many pixels of frame n are the glitch's red split: red, no green."""
    raw = frame_rgba(clip, n)
    return sum(1 for i in range(0, len(raw), 4)
               if raw[i + 3] > 120 and raw[i] > 180 and raw[i + 1] < 60 and raw[i + 2] < 60)


def test_looks(tmp: Path) -> None:
    print("the looks")
    log = tmp / "log.txt"
    check("text_color: a name", text_color({"text_color": "Cyan"}) == COLORS["cyan"])
    check("text_color: #rrggbb", text_color({"text_color": "#10ff80"}) == (16, 255, 128))
    check("text_color: unknown is amber", text_color({"text_color": "plaid"}) == AMBER
          and text_color({}) == AMBER)
    check("text_animation: unknown is slam", text_animation({"text_animation": "spin"}) == "slam"
          and text_animation({}) == "slam" and text_animation({"text_animation": "Pop"}) == "pop")
    check("pop grows from 0.2x and springs past 1.0x",
          abs(pop_scale_at(0) - 0.2) < 1e-9 and pop_scale_at(0.18) > 1.1
          and pop_scale_at(1.0) == 1.0)

    slam, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log)
    settled_w = alpha_of(slam, 40)[1]
    pop, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log, animation="pop")
    check("pop: starts small", alpha_of(pop, 1)[1] < settled_w * 0.6,
          f"{alpha_of(pop, 1)[1]} vs {settled_w}")
    check("pop: settles at the same size", abs(alpha_of(pop, 40)[1] - settled_w) <= 3)
    typed, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log, animation="type")
    first_w, all_w = alpha_of(typed, 0)[1], alpha_of(typed, 40)[1]
    check("type: one letter first, all of them later", first_w < all_w * 0.5,
          f"{first_w} vs {all_w}")
    glitch, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log, animation="glitch")
    burst = max(red_fringe(glitch, n) for n in (2, 5, 8, 11))
    check("glitch: a red split while it breaks up", burst > 200, f"{burst} px")
    check("glitch: clean once it's in", red_fringe(glitch, 40) < 20,
          f"{red_fringe(glitch, 40)} px")
    again, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log, animation="glitch")
    check("glitch: the same jitter every time (cached)", again == glitch)
    check("each animation is its own clip", len({slam, pop, typed, glitch}) == 4)

    cyan, _, _ = kinetic_clip("ACE", 110, 1.0, tmp / "k", log, accent=COLORS["cyan"])
    r, _, b = glow_tint(cyan, 8)
    ra, _, ba = glow_tint(slam, 8)
    check("the glow takes the accent color", b > r and ra > ba,
          f"cyan glow {glow_tint(cyan, 8)}, amber glow {glow_tint(slam, 8)}")

    fx = {**PLAIN_FX, "text_color": "magenta", "text_animation": "pop"}
    tags = [(1.0, "NOSCOPE")]
    looked, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision("3K"), fx, None,
                                 tags=tags, kinetic_dir=tmp / "k", log_path=log)
    default, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision("3K"), PLAIN_FX, None,
                                  tags=tags, kinetic_dir=tmp / "k", log_path=log)
    check("the engine draws the text in the configured look",
          looked.count("movie=") == 2 and looked != default)
    plain_tags, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision(None), fx, None, tags=tags)
    check("kill tags without Pillow: the accent color too",
          "fontcolor=0xFF54D4" in plain_tags)


def test_graph(tmp: Path) -> None:
    print("the filter graph")
    log = tmp / "log.txt"
    counter = [(1.0, "1"), (2.0, "2"), (3.0, "3K")]
    graph, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision("3K"), PLAIN_FX, None,
                                counter=counter, kinetic_dir=tmp / "k", log_path=log)
    check("overlay text + 3 ticks = 4 movie sources", graph.count("movie=") == 4)
    check("no drawtext for them", "drawtext" not in graph)
    check("each placed at its start",
          all(f"setpts=PTS+{s:.3f}/TB" in graph for s in (0.4, 1.0, 2.0, 3.0)))
    check("only the last tick holds", graph.count("eof_action=repeat") == 1
          and graph.count("eof_action=pass") == 3)
    check("ends in yuv420p", "format=yuv420p[vout]" in graph)
    boxed, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision("3K"), PLAIN_FX, None,
                                letterbox=True, kinetic_dir=tmp / "k", log_path=log)
    check("with letterbox the bar goes on top of the text",
          boxed.index("movie=") < boxed.index("[vfx][lbb]overlay"))
    plain, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision("3K"), PLAIN_FX, None,
                                counter=counter)
    check("without kinetic_dir: drawtext as before", "movie=" not in plain
          and plain.count("drawtext") == 4)

    tags = [(1.0, "NOSCOPE"), (2.5, "WALLBANG  ·  42 M")]
    tagged, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision(None), PLAIN_FX, None,
                                 tags=tags, kinetic_dir=tmp / "k", log_path=log)
    check("kill tags: one movie source each", tagged.count("movie=") == 2)
    check("kill tags: no drawtext", "drawtext" not in tagged)
    check("kill tags: placed on their kill",
          all(f"setpts=PTS+{s:.3f}/TB" in tagged for s, _ in tags))
    check("kill tags: none of them holds", tagged.count("eof_action=pass") == 2
          and "eof_action=repeat" not in tagged)
    plain_tags, _, _ = _build_filter([(0.0, 4.0, 1.0)], decision(None), PLAIN_FX, None,
                                     tags=tags)
    check("kill tags without kinetic_dir: amber drawtext",
          "movie=" not in plain_tags and plain_tags.count("fontcolor=0xFFB347") == 2)


def test_window(tmp: Path) -> None:
    print("the text in the picture")
    log = tmp / "log.txt"
    src = tmp / "src.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=s=1280x720:r=30:d=4", "-f", "lavfi", "-i", "sine=d=4",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                    str(src)], check=True)

    def render(name: str, text: str | None, kinetic: bool) -> Path:
        graph, vout, aout = _build_filter(
            [(0.0, 4.0, 1.0)], decision(text), PLAIN_FX, None,
            kinetic_dir=(tmp / "k") if kinetic else None, log_path=log)
        out = tmp / name
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", graph,
                        "-map", vout, "-map", aout, "-t", "4", "-c:v", "libx264",
                        "-preset", "ultrafast", "-crf", "12", str(out)], check=True)
        return out

    bare, text = render("bare.mp4", None, False), render("text.mp4", "3K", True)
    before = diff(gray_frame(bare, 0.2), gray_frame(text, 0.2))
    during = diff(gray_frame(bare, 1.5), gray_frame(text, 1.5))
    after = diff(gray_frame(bare, 3.6), gray_frame(text, 3.6))
    check("nothing before 0.4 s", before < 0.5, f"{before:.2f}")
    check("the text is there at 1.5 s", during > 8.0, f"{during:.2f}")
    check("gone after 3.2 s", after < 0.5, f"{after:.2f}")


if __name__ == "__main__":
    test_curve()
    with tempfile.TemporaryDirectory() as t:
        test_clip(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_looks(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_graph(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_window(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All kinetic text tests OK")
