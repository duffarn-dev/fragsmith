"""P21-tester for lib/beats.py: drop-deteksjon + shift_beats.

Kjør: python tests/test_p21_beats.py
"""
from __future__ import annotations

import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from lib.beats import (  # noqa: E402
    _detect_drops,
    analyze_beats,
    nearest_beat_before,
    shift_beats,
)

SR = 22050
FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FEIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def synth_drop_signal(quiet_s: float = 15.0, loud_s: float = 15.0):
    """Stille oppbygging → høy seksjon ved t=quiet_s (klassisk drop)."""
    rng = np.random.default_rng(42)
    quiet = 0.03 * rng.standard_normal(int(quiet_s * SR))
    t = np.arange(int(loud_s * SR)) / SR
    loud = 0.35 * rng.standard_normal(len(t)) + 0.3 * np.sin(2 * np.pi * 55 * t)
    return np.concatenate([quiet, loud]).astype(np.float32), quiet_s


def test_detect_drops() -> None:
    print("_detect_drops:")
    y, drop_at = synth_drop_signal()
    beat_grid = [round(t, 3) for t in np.arange(0.5, 29.5, 0.5)]

    drops = _detect_drops(y, SR, beat_grid)
    check("finner drop i stille→høy-signal", len(drops) >= 1)
    if drops:
        best = drops[0]
        check(
            "sterkeste drop ligger på overgangen (±1.0 s)",
            abs(best["time_s"] - drop_at) <= 1.0,
            f"funnet {best['time_s']}s, fasit {drop_at}s",
        )
        check("strength er normalisert til 1.0 for sterkeste", best["strength"] == 1.0)
        gaps_ok = all(
            abs(a["time_s"] - b["time_s"]) >= 8.0
            for i, a in enumerate(drops)
            for b in drops[i + 1:]
        )
        check("non-max suppression (>= 8 s mellom drops)", gaps_ok)

    # konstant signal: ingen kontrast → ingen drops
    rng = np.random.default_rng(7)
    flat = (0.2 * rng.standard_normal(30 * SR)).astype(np.float32)
    check("flatt signal gir ingen drops", _detect_drops(flat, SR, beat_grid) == [])


def test_analyze_beats_end_to_end() -> None:
    print("analyze_beats (ende-til-ende med syntetisk wav):")
    y, drop_at = synth_drop_signal()
    # klikk hvert 0.5 s (120 BPM) så beat_track har noe å bite i
    clicks = np.zeros_like(y)
    for t in np.arange(0.5, len(y) / SR, 0.5):
        i = int(t * SR)
        clicks[i:i + 220] += 0.5 * np.sin(2 * np.pi * 1500 * np.arange(220) / SR)
    y = np.clip(y + clicks, -1, 1)

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "drop.wav"
        with wave.open(str(wav), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(SR)
            fh.writeframes((y * 32767).astype(np.int16).tobytes())
        beats = analyze_beats(wav)

    check("drops-felt finnes", "drops" in beats)
    check("finner drop i wav", len(beats["drops"]) >= 1)
    if beats["drops"]:
        best = beats["drops"][0]
        check(
            "sterkeste drop nær overgangen (±3 s)",
            abs(best["time_s"] - drop_at) <= 3.0,
            f"funnet {best['time_s']}s, fasit {drop_at}s",
        )


def test_shift_beats() -> None:
    print("shift_beats:")
    beats = {
        "bpm": 120.0,
        "beat_times": [0.5, 1.0, 10.0, 20.0],
        "drops": [{"time_s": 20.0, "strength": 1.0, "ratio": 2.0}],
    }
    s = shift_beats(beats, 9.0)
    check("beats før offset kuttes", s["beat_times"] == [1.0, 11.0])
    check("drops forskyves", s["drops"][0]["time_s"] == 11.0)
    check("music_offset_s lagres", s["music_offset_s"] == 9.0)
    check("offset 0 er no-op (samme objekt)", shift_beats(beats, 0.0) is beats)
    check("original urørt", beats["beat_times"] == [0.5, 1.0, 10.0, 20.0])
    # dobbel forskyvning akkumulerer offset
    s2 = shift_beats(s, 2.0)
    check("akkumulert offset", s2["music_offset_s"] == 11.0)

    # regresjon: nearest_beat_before uendret
    check(
        "nearest_beat_before-regresjon",
        nearest_beat_before(10.3, beats, 0.75) == 10.0
        and nearest_beat_before(9.0, beats, 0.75) is None,
    )


if __name__ == "__main__":
    test_detect_drops()
    test_analyze_beats_end_to_end()
    test_shift_beats()
    print()
    if FAILS:
        print(f"FEILET: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("Alle P21-beat-tester OK")
