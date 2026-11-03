"""Shared ffmpeg runner with full logging (command + exit code + stderr).

All ffmpeg work goes through run_ffmpeg() so every command is logged
verbatim to runs/<id>/ffmpeg.log, and failures stop the run with the
full stderr (never silently swallowed).
"""
from __future__ import annotations

import re
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
    """Source frame rate. Above 60 means a high-fps rendered clip, so slow-mo
    can use real frames instead of minterpolate."""
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


def probe_video_start(path: Path) -> float:
    """When the first video frame is shown (stream start_time), 0 if unknown."""
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=start_time",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise FfmpegError(f"ffprobe failed on {path}: {proc.stderr.strip()}")
    try:
        return max(0.0, float(proc.stdout.strip()))
    except ValueError:  # "N/A"
        return 0.0


def mean_volume(path: Path, start: float = 0.0, seconds: float | None = None) -> float:
    """The RMS level of a stretch of a file's audio in dBFS (volumedetect's
    mean_volume), -91 for silence."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{max(0.0, start):.3f}"]
    if seconds is not None:
        cmd += ["-t", f"{seconds:.3f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise FfmpegError(f"volumedetect failed on {path}: {proc.stderr.strip()[-500:]}")
    found = re.search(r"mean_volume: (-?[\d.]+) dB", proc.stderr)
    return float(found.group(1)) if found else -91.0


def drawtext_escape(text: str) -> str:
    """Escape text for use in drawtext=text=...

    Note (verified 2026-07-05):
    - every drawtext call MUST use :expansion=none. There is no working
      escape for a literal '%' with expansion=normal ("Stray %").
    - an ASCII apostrophe can NOT be escaped reliably through both the
      filtergraph AND the drawtext parsing level (the old \\\\\\' broke the
      graph when later args had commas; the '\\'' trick gets eaten by
      drawtext). It's replaced with a typographic ’, which renders nicely
      and is parser-neutral."""
    return (
        text.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "’")
    )


FONT_FILE = "C\\:/Windows/Fonts/arialbd.ttf"  # colon escaped for filter expressions
