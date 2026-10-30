"""Felles ffmpeg-kjøring med full logging (kommando + exit code + stderr).

Alt ffmpeg-arbeid går via run_ffmpeg() så observability-kravet holdes:
hver kommando logges ordrett til runs/<id>/ffmpeg.log, og feil stopper
kjøringen med full stderr (aldri stille svelging).
"""
from __future__ import annotations

import subprocess
import time
from datetime import datetime
from pathlib import Path


class FfmpegError(RuntimeError):
    pass


def run_ffmpeg(args: list[str], log_path: Path, description: str) -> None:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    elapsed = time.monotonic() - t0
    with open(log_path, "a", encoding="utf-8") as log:
        log.write(
            f"\n[{datetime.now():%H:%M:%S}] {description}\n"
            f"  cmd: {subprocess.list2cmdline(cmd)}\n"
            f"  exit: {proc.returncode} ({elapsed:.1f} s)\n"
        )
        if proc.stderr.strip():
            log.write(f"  stderr:\n{proc.stderr}\n")
    if proc.returncode != 0:
        raise FfmpegError(
            f"ffmpeg failed ({description}), exit {proc.returncode}.\n"
            f"Command: {subprocess.list2cmdline(cmd)}\n"
            f"stderr: {proc.stderr.strip()[-2000:]}"
        )


def probe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    return float(proc.stdout.strip())


def probe_fps(path: Path) -> float:
    """Kildens framerate (P20: >60 betyr høy-fps-rendret klipp — slow-mo
    kan da bremses med ekte frames i stedet for minterpolate)."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    num, _, den = proc.stdout.strip().partition("/")
    return float(num) / float(den or 1)


def drawtext_escape(text: str) -> str:
    """Escape tekst for bruk i drawtext=text=...

    NB (verifisert 2026-07-05):
    - alle drawtext-kall MÅ ha :expansion=none — det finnes ingen fungerende
      escape for literal '%' med expansion=normal ("Stray %").
    - ASCII-apostrof kan IKKE escapes robust gjennom filtergraf- OG
      drawtext-parsenivået (gammel \\\\\\' knakk grafen når senere args hadde
      kommaer; '\\''-trikset spises av drawtext). Byttes til typografisk ’,
      som rendres pent og er parser-nøytral."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")
    )


FONT_FILE = "C\\:/Windows/Fonts/arialbd.ttf"  # kolon escapet for filteruttrykk
