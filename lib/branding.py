"""Branding: your own images behind the intro/outro cards, and a logo on the film.

Paths in [branding] are relative to the project folder unless absolute. A file
that's missing never stops a film: it's reported as a flag and left out.

The logo goes on in the concat step, not per segment: one place covers every
part of the film (cold open, cards, clips, replay), and its position comes from
the frame's own size, so a 9:16 film can reuse it. The price is one re-encode
of the timeline, and only when a logo is set.
"""
from __future__ import annotations

from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
POSITIONS = ("top-left", "top-right", "bottom-left", "bottom-right")
IMAGE_KEYS = ("intro_image", "outro_image", "logo")


def _resolve(value: str) -> Path:
    path = Path(value)
    # a relative path is anchored in the project folder, not the CWD
    return path if path.is_absolute() else PROJECT_DIR / path


def branding_files(cfg: dict) -> tuple[dict[str, Path | None], list[str]]:
    """The [branding] files that exist, plus a warning for each one that's
    set but missing (and for an unknown logo_position)."""
    b = cfg.get("branding", {})
    files: dict[str, Path | None] = {}
    warnings: list[str] = []
    for key in IMAGE_KEYS:
        value = str(b.get(key, "") or "").strip()
        if not value:
            files[key] = None
            continue
        path = _resolve(value)
        if path.is_file():
            files[key] = path
        else:
            files[key] = None
            warnings.append(f"[branding].{key} not found: {value} (the film is made without it)")
    pos = str(b.get("logo_position", "bottom-right"))
    if files["logo"] and pos not in POSITIONS:
        warnings.append(
            f"[branding].logo_position '{pos}' is unknown, so bottom-right is used "
            f"(use {', '.join(POSITIONS)})"
        )
    return files, warnings


def card_background_filter(w: int, h: int) -> str:
    """Fill the frame whatever the image's aspect (scale to cover, crop the
    center), then darken it so white card text stays readable."""
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1,"
        f"drawbox=c=black@0.35:t=fill"
    )


def logo_overlay(
    branding: dict, frame_w: int, main: str = "[0:v]", logo: str = "[1:v]", out: str = "[v]",
    frame_h: int | None = None,
) -> str:
    """filter_complex chain that puts the logo in a corner of `main`.

    The logo input must be looped (`-loop 1 -i logo.png`): a single-frame
    input stopped showing after the first concat part. shortest=1 ends the
    looped logo with the film.

    Ends in yuv420p on purpose: overlaying an RGBA png lets ffmpeg pick gbrp
    for the output, which libx264 encodes as RGB and players show purple and
    green (found 2026-11-01)."""
    size = min(0.5, max(0.02, float(branding.get("logo_size", 0.12))))
    opacity = min(1.0, max(0.0, float(branding.get("logo_opacity", 0.8))))
    pos = str(branding.get("logo_position", "bottom-right"))
    if pos not in POSITIONS:
        pos = "bottom-right"
    margin = round(frame_w * 0.025)
    # a share of the long side: the same width in px in 16:9 and 9:16, so the
    # logo isn't half as big on a phone held upright
    long_side = max(frame_w, frame_h or 0)
    logo_w = max(2, round(long_side * size / 2) * 2)
    x = str(margin) if pos.endswith("left") else f"W-w-{margin}"
    if frame_h and frame_h > frame_w:
        # 9:16: captions and buttons cover the bottom of a Short, so the logo
        # goes to the top, below the app bar (there's no killfeed up there)
        y = str(round(frame_h * 0.10))
    else:
        y = str(margin) if pos.startswith("top") else f"H-h-{margin}"
    return (
        f"{logo}scale={logo_w}:-1,format=rgba,colorchannelmixer=aa={opacity:.2f}[lg];"
        f"{main}[lg]overlay=x={x}:y={y}:format=auto:shortest=1,format=yuv420p{out}"
    )
