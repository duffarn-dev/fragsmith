"""Kinetic text: the overlay text and the kill counter slam in instead of
switching on.

Each text is drawn frame by frame with Pillow (it overshoots from 1.9x, glows
amber on the slam and settles), packed into a short .mov with alpha, and laid
over the segment by the editing engine with ffmpeg's `movie` source. drawtext
can't scale text smoothly or blur a glow, which is why this isn't a filter.

Without Pillow the engine keeps using drawtext ([effects].kinetic_text is
then flagged in the report).
"""
from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from lib.ffmpeg_util import FONT_FILE, run_ffmpeg

FONT_PATH = FONT_FILE.replace("\\:", ":")  # FONT_FILE is escaped for filters
FPS = 60
AMBER = (255, 179, 71)
SLAM_S = 0.12    # from 1.9x down past the target, to 0.94x
SETTLE_S = 0.22  # back up to 1.0x
OUT_S = 0.3      # the fade-out, while the letters spread apart
# Bump when the look changes, so clips cached in a run folder are redrawn
LOOK_VERSION = 2


def available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _ease_out(x: float) -> float:
    return 1 - (1 - x) ** 3


def scale_at(t: float) -> float:
    """The text's size at t seconds, relative to its final size."""
    if t < SLAM_S:
        return 1.9 - (1.9 - 0.94) * _ease_out(t / SLAM_S)
    if t < SETTLE_S:
        return 0.94 + 0.06 * _ease_out((t - SLAM_S) / (SETTLE_S - SLAM_S))
    return 1.0


def glow_at(t: float) -> float:
    """How strong the glow is: building to full on the slam, then settling."""
    if t < SLAM_S:
        return t / SLAM_S
    return max(0.4, 1.0 - (t - SLAM_S) / 0.5)


def _stroke(size: int) -> int:
    return max(3, size // 18)


def text_box(text: str, size: int) -> tuple[int, int]:
    """Width and height of the settled text, stroke included."""
    from PIL import ImageFont

    font = ImageFont.truetype(FONT_PATH, size)
    left, top, right, bottom = font.getbbox(text, stroke_width=_stroke(size))
    return right - left, bottom - top


def kinetic_clip(
    text: str,
    size: int,
    seconds: float,
    out_dir: Path,
    log_path: Path,
    fade_out: bool = True,
) -> tuple[Path, int, int]:
    """The text's animation as a .mov with alpha at 60 fps, plus its canvas
    size. The canvas is centered on the text and 2.1x its box, so the
    overshoot is never cut off. Drawn once per text, size, length and look:
    the 9:16 pass and a re-run of `finish` reuse it.

    fade_out=False keeps the last frame settled, for a text the overlay
    holds until the segment ends (the counter's last tick)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    tw, th = text_box(text, size)
    cw, ch = int(tw * 2.1) + 80, int(th * 2.1) + 80
    cw += cw % 2
    ch += ch % 2
    key = hashlib.sha1(
        f"{LOOK_VERSION}|{text}|{size}|{seconds:.3f}|{fade_out}".encode("utf-8")
    ).hexdigest()[:12]
    clip = out_dir / f"text_{key}.mov"
    if clip.exists():
        return clip, cw, ch

    frames_dir = out_dir / f"text_{key}_frames"
    frames_dir.mkdir(exist_ok=True)
    n = max(1, round(seconds * FPS))
    out_start = seconds - OUT_S if fade_out else seconds + 1
    for i in range(n):
        t = i / FPS
        s = scale_at(t)
        out_k = min(1.0, max(0.0, (t - out_start) / OUT_S))
        # in over 3 frames: a slower fade hid the biggest sizes of the slam
        alpha = min(1.0, (i + 1) / 3) * (1 - out_k)
        font = ImageFont.truetype(FONT_PATH, max(4, round(size * s)))
        stroke = max(2, round(_stroke(size) * s))
        # letter by letter, so the spacing can open up on the way out
        advances = [font.getlength(c) for c in text]
        spacing = 0.12 * out_k * size * s
        x = (cw - sum(advances) - spacing * (len(text) - 1)) / 2
        layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for c, adv in zip(text, advances):
            draw.text((x, ch / 2), c, font=font, fill=(255, 255, 255, 255),
                      anchor="lm", stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += adv + spacing
        # The glow is the text's shape grown and blurred at quarter size
        # (16x fewer pixels to blur), in amber, under the text
        small = layer.getchannel("A").resize((cw // 4, ch // 4), Image.BILINEAR)
        mask = small.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(4))
        g = glow_at(t) * (1 - out_k)
        mask = mask.point(lambda v, g=g: min(255, int(v * 1.6 * g)))
        glow = Image.new("RGBA", mask.size, AMBER + (0,))
        glow.putalpha(mask)
        frame = Image.alpha_composite(glow.resize((cw, ch), Image.BILINEAR), layer)
        if alpha < 1:
            frame.putalpha(frame.getchannel("A").point(lambda v, a=alpha: int(v * a)))
        frame.save(frames_dir / f"{i:04d}.png", compress_level=1)

    run_ffmpeg(
        [
            "-framerate", str(FPS), "-i", str(frames_dir / "%04d.png"),
            "-c:v", "png", "-pix_fmt", "rgba", str(clip),
        ],
        log_path,
        f"step 6: kinetic text '{text}'",
    )
    shutil.rmtree(frames_dir, ignore_errors=True)
    return clip, cw, ch


def movie_source(clip: Path) -> str:
    """The clip's path as a `movie` source argument (colon escaped, quoted)."""
    return "movie='" + clip.as_posix().replace(":", "\\:") + "'"
