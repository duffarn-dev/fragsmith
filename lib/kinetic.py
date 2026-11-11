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
import math
import random
import shutil
from pathlib import Path

from lib.ffmpeg_util import FONT_FILE, run_ffmpeg

FONT_PATH = FONT_FILE.replace("\\:", ":")  # FONT_FILE is escaped for filters
FPS = 60
AMBER = (255, 179, 71)
WHITE = (255, 255, 255)
SLAM_S = 0.12    # from 1.9x down past the target, to 0.94x
SETTLE_S = 0.22  # back up to 1.0x
OUT_S = 0.3      # the fade-out, while the letters spread apart
# [effects].text_color: the glow, and the kill tags' letters. Or "#rrggbb"
COLORS = {
    "amber": AMBER,
    "red": (255, 72, 72),
    "cyan": (72, 224, 255),
    "magenta": (255, 84, 212),
    "lime": (170, 255, 84),
    "gold": (255, 212, 64),
    "white": WHITE,
}
# [effects].text_animation: how a text comes in
ANIMATIONS = ("slam", "pop", "glitch", "type")
POP_S = 0.18     # pop: from 0.2x up past the target, to 1.15x
TYPE_S = 0.3     # type: the letters appear one by one over at most this
GLITCH_IN_S = 0.3  # glitch: the burst it comes in with (and goes out with)
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


def text_color(effects: dict) -> tuple[int, int, int]:
    """The accent color from [effects].text_color; amber when unknown."""
    name = str(effects.get("text_color", "amber")).strip().lower()
    if name in COLORS:
        return COLORS[name]
    if len(name) == 7 and name.startswith("#"):
        try:
            return tuple(int(name[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return AMBER


def text_animation(effects: dict) -> str:
    """The animation from [effects].text_animation; slam when unknown."""
    name = str(effects.get("text_animation", "slam")).strip().lower()
    return name if name in ANIMATIONS else "slam"


def pop_scale_at(t: float) -> float:
    """pop: grows from small with a springy overshoot, instead of slamming."""
    if t < POP_S:
        return 0.2 + (1.15 - 0.2) * _ease_out(t / POP_S)
    if t < POP_S + 0.12:
        return 1.15 - 0.18 * _ease_out((t - POP_S) / 0.12)
    if t < POP_S + 0.24:
        return 0.97 + 0.03 * _ease_out((t - POP_S - 0.12) / 0.12)
    return 1.0


def pop_angle_at(t: float) -> float:
    """pop: a wobble that dies out, in degrees."""
    return 7.0 * math.sin(2 * math.pi * t / 0.22) * math.exp(-t / 0.15)


def glitching(t: float, seconds: float, fade_out: bool) -> bool:
    """glitch: the frames that break up. A burst coming in, a flicker in the
    middle of a long text, and a burst going out."""
    if t < GLITCH_IN_S:
        return True
    if seconds >= 1.5 and abs(t - seconds * 0.55) < 0.05:
        return True
    return fade_out and t > seconds - 0.15


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
    fill: tuple[int, int, int] = WHITE,
    accent: tuple[int, int, int] = AMBER,
    animation: str = "slam",
) -> tuple[Path, int, int]:
    """The text's animation as a .mov with alpha at 60 fps, plus its canvas
    size. The canvas is centered on the text and 2.1x its box, so the
    overshoot is never cut off. Drawn once per text, size, length and look:
    the 9:16 pass and a re-run of `finish` reuse it.

    fade_out=False keeps the last frame settled, for a text the overlay
    holds until the segment ends (the counter's last tick). fill is the
    letters' color (the kill tags are amber), accent the glow's, and
    animation one of ANIMATIONS."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    out_dir.mkdir(parents=True, exist_ok=True)
    tw, th = text_box(text, size)
    cw, ch = int(tw * 2.1) + 80, int(th * 2.1) + 80
    cw += cw % 2
    ch += ch % 2
    # white keeps the key it had before fill existed, so cached clips stay valid
    look = f"{LOOK_VERSION}|{text}|{size}|{seconds:.3f}|{fade_out}"
    if fill != WHITE:
        look += f"|{fill}"
    # the defaults keep their keys too
    if accent != AMBER or animation != "slam":
        look += f"|{accent}|{animation}"
    key = hashlib.sha1(look.encode("utf-8")).hexdigest()[:12]
    clip = out_dir / f"text_{key}.mov"
    if clip.exists():
        return clip, cw, ch

    frames_dir = out_dir / f"text_{key}_frames"
    frames_dir.mkdir(exist_ok=True)
    n = max(1, round(seconds * FPS))
    out_start = seconds - OUT_S if fade_out else seconds + 1
    # the glitch's jitter is the same on every run, so cached clips match
    rng = random.Random(key)
    per_letter = min(0.05, TYPE_S / max(1, len(text)))
    for i in range(n):
        t = i / FPS
        s = {"slam": scale_at, "pop": pop_scale_at}.get(animation, lambda _t: 1.0)(t)
        out_k = min(1.0, max(0.0, (t - out_start) / OUT_S))
        # in over 3 frames: a slower fade hid the biggest sizes of the slam
        alpha = min(1.0, (i + 1) / 3) * (1 - out_k)
        font = ImageFont.truetype(FONT_PATH, max(4, round(size * s)))
        stroke = max(2, round(_stroke(size) * s))
        # letter by letter, so the spacing can open up on the way out
        advances = [font.getlength(c) for c in text]
        spacing = 0.12 * out_k * size * s
        # type: the letters appear one at a time where they'll end up
        shown = len(text) if animation != "type" else min(len(text), int(t / per_letter) + 1)
        x = (cw - sum(advances) - spacing * (len(text) - 1)) / 2
        layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        draw = ImageDraw.Draw(layer)
        for c, adv in zip(text[:shown], advances):
            draw.text((x, ch / 2), c, font=font, fill=fill + (255,),
                      anchor="lm", stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
            x += adv + spacing
        if animation == "pop":
            layer = layer.rotate(pop_angle_at(t), resample=Image.BICUBIC)
        broken = animation == "glitch" and glitching(t, seconds, fade_out)
        if broken:
            layer = _glitch(layer, size, rng)
        # The glow is the text's shape grown and blurred at quarter size
        # (16x fewer pixels to blur), in the accent color, under the text
        small = layer.getchannel("A").resize((cw // 4, ch // 4), Image.BILINEAR)
        mask = small.filter(ImageFilter.MaxFilter(3)).filter(ImageFilter.GaussianBlur(4))
        g = glow_at(t) * (1 - out_k)
        if animation == "type" and t < per_letter * len(text):
            g = 0.6 + 0.4 * (1 - (t % per_letter) / per_letter)  # a pulse per letter
        mask = mask.point(lambda v, g=g: min(255, int(v * 1.6 * g)))
        glow = Image.new("RGBA", mask.size, accent + (0,))
        glow.putalpha(mask)
        frame = Image.alpha_composite(glow.resize((cw, ch), Image.BILINEAR), layer)
        if broken and rng.random() < 0.15:
            alpha = 0.0  # a dropped frame: the text flickers out
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


def _glitch(layer, size: int, rng: random.Random):
    """One broken frame: the red and the cyan of the text split sideways,
    and a few horizontal slices slip."""
    from PIL import Image, ImageChops

    w, h = layer.size
    a = layer.getchannel("A")
    dx = max(2, size // 14)
    red = Image.merge("RGBA", (a, Image.new("L", (w, h)), Image.new("L", (w, h)), a))
    cyan = Image.merge("RGBA", (Image.new("L", (w, h)), a, a, a))
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    out.alpha_composite(ImageChops.offset(red, -dx, 0))
    out.alpha_composite(ImageChops.offset(cyan, dx, 0))
    out.alpha_composite(layer)
    for _ in range(rng.randint(2, 4)):
        y0 = rng.randrange(0, h)
        band = out.crop((0, y0, w, min(h, y0 + rng.randint(h // 16, h // 6))))
        out.paste(Image.new("RGBA", band.size, (0, 0, 0, 0)), (0, y0))
        # wraps around, but the canvas is 2x the text, so the edge is empty
        out.alpha_composite(ImageChops.offset(band, rng.randint(-size // 4, size // 4), 0), (0, y0))
    return out


def movie_source(clip: Path) -> str:
    """The clip's path as a `movie` source argument (colon escaped, quoted)."""
    return "movie='" + clip.as_posix().replace(":", "\\:") + "'"
