"""Tests for the drop cues in lib/assemble.py: a riser into the drop, a gap of
silence just before it and a boom on it.

Covers: where the cues are anchored (the drop, else the climax clip), their
timing from the BPM, what turns them off, the synthesized riser and boom, and
the real mix measured in time windows: louder before the gap, silent in it,
and bass on the drop.

Run: python tests/test_sound_design.py
"""
from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.assemble import (  # noqa: E402
    _ensure_sfx,
    _text_card,
    assemble,
    drop_cue,
    drop_cue_times,
)
from lib.ffmpeg_util import probe_duration  # noqa: E402
from lib.models import EditDecision, EditPlan  # noqa: E402
from lib.styles import apply_style  # noqa: E402

FAILS: list[str] = []
ALL_ON = {"riser": True, "drop_gap": True, "drop_boom": True}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


def samples(path: Path, af: str = "anull") -> list[float]:
    """48 kHz float samples of a file's left channel. Not a mono downmix:
    that sums the channels at -3 dB each, so identical channels come out
    3 dB louder (and peaks over 1.0)."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-af", f"{af},pan=mono|c0=c0",
         "-ar", "48000", "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def rms_db(s: list[float], start: float, end: float) -> float:
    part = s[int(start * 48000):int(end * 48000)]
    if not part:
        return -200.0
    rms = math.sqrt(sum(x * x for x in part) / len(part))
    return 20 * math.log10(rms) if rms > 0 else -200.0


def plan(*ids: str) -> EditPlan:
    return EditPlan(
        demo_path="x.dem", music_track="m.wav", intro_text="INTRO", outro_text="OUTRO",
        decisions=[
            EditDecision(clip_id=c, order=i + 1, slowmo_segments=[], overlay_text=None,
                         transition_in="cut", reasoning="test")
            for i, c in enumerate(ids)
        ],
        dropped_clip_ids=[],
    )


def test_times() -> None:
    print("cue timing")
    on = {"editing": dict(ALL_ON)}
    t = drop_cue_times({"time_s": 30.0, "on_drop": True, "bpm": 128}, on)
    check("128 BPM: the gap is 0.1 s (1/8 beat is shorter than the minimum)",
          t.get("gap") == (29.9, 30.0), str(t.get("gap")))
    check("128 BPM: the riser is 2 bars (3.75 s) and ends where the gap starts",
          t.get("riser") == (26.15, 29.9), str(t.get("riser")))
    check("the boom is on the drop", t.get("boom") == 30.0 and t.get("at") == 30.0)

    t = drop_cue_times({"time_s": 30.0, "on_drop": True, "bpm": 60}, on)
    check("60 BPM: the gap is 1/8 beat", t.get("gap") == (29.875, 30.0), str(t.get("gap")))
    check("60 BPM: the riser stops at 4 s", t.get("riser") == (25.875, 29.875), str(t.get("riser")))

    t = drop_cue_times({"time_s": 30.0, "on_drop": True, "bpm": None}, on)
    check("no BPM: a 0.5 s beat is assumed",
          t.get("gap") == (29.9, 30.0) and t.get("riser") == (25.9, 29.9), str(t))

    t = drop_cue_times({"time_s": 20.0, "on_drop": False, "bpm": 128}, on)
    check("no real drop: no gap, the riser ends on the climax",
          "gap" not in t and t.get("riser") == (16.25, 20.0), str(t))

    t = drop_cue_times({"time_s": 1.2, "on_drop": True, "bpm": 128}, on)
    check("too early for a riser: none, but the gap and the boom stay",
          "riser" not in t and "gap" in t and "boom" in t, str(t))

    cue = {"time_s": 30.0, "on_drop": True, "bpm": 128}
    check("all off: nothing", drop_cue_times(cue, {"editing": {}}) == {})
    check("[effects].enabled = false: nothing",
          drop_cue_times(cue, {"editing": dict(ALL_ON), "effects": {"enabled": False}}) == {})
    check("no cue: nothing", drop_cue_times(None, on) == {})
    t = drop_cue_times(cue, {"editing": {"drop_boom": True}})
    check("only the boom", set(t) == {"boom", "at"}, str(t))


def test_anchor() -> None:
    print("cue anchor")
    p = plan("a", "b", "c")
    cut_log = [
        {"clip_id": "a", "timeline_end_s": 8.0, "segment_duration_s": 4.0},
        {"clip_id": "b", "timeline_end_s": 12.0, "segment_duration_s": 4.0},
        {"clip_id": "c", "timeline_end_s": 17.5, "segment_duration_s": 5.5},
    ]
    drop = {"drop_timeline_s": 12.02}
    check("with drop sync: on the music's drop",
          drop_cue(p, cut_log, drop, 128) == {"time_s": 12.02, "on_drop": True, "bpm": 128})
    check("without: the start of the climax clip (the last one)",
          drop_cue(p, cut_log, None, 128) == {"time_s": 12.0, "on_drop": False, "bpm": 128})
    p.decisions[1].is_climax = True
    check("the Director's climax wins", drop_cue(p, cut_log, None, 128)["time_s"] == 8.0)
    check("climax clip not in the cut: None", drop_cue(p, cut_log[:1], None, 128) is None)


def test_synth(tmp: Path) -> None:
    print("synthesized riser and boom")
    sfx = _ensure_sfx(tmp / "sfx", tmp / "log.txt")
    check("all four files", all(p.exists() for p in sfx.values()), str(sorted(sfx)))
    check("riser.wav is 4 s", abs(probe_duration(sfx["riser"]) - 4.0) < 0.05)
    check("boom.wav is 1.8 s", abs(probe_duration(sfx["boom"]) - 1.8) < 0.05)
    r = samples(sfx["riser"])
    early, late = rms_db(r, 0.5, 0.8), rms_db(r, 3.6, 3.9)
    check("the riser swells by 20 dB or more", late - early >= 20, f"{early:.1f} → {late:.1f} dB")
    check("the riser is still rising in its last second",
          rms_db(r, 3.7, 3.95) > rms_db(r, 2.9, 3.15) + 1)
    check("no clipping", max(abs(x) for x in r) < 0.99
          and max(abs(x) for x in samples(sfx["boom"])) < 0.99)
    low = samples(sfx["boom"], "lowpass=f=150")
    check("the boom is mostly bass", rms_db(low, 0, 0.5) > rms_db(samples(sfx["boom"]), 0, 0.5) - 3)


def test_mix(tmp: Path) -> None:
    print("the mix, measured")
    log = tmp / "log.txt"
    work = tmp / "04-segments"
    work.mkdir()
    segs = []
    for i, secs in enumerate((4.0, 4.0, 3.0)):
        seg = work / f"seg{i}.mp4"
        _text_card(f"CLIP {i}", secs, seg, log, "test segment")
        segs.append(seg)
    # constant music at -15 dB RMS (the sine source is -18 dB peak by
    # default), so every cue stands out against it
    music = tmp / "music.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=f=440:d=20:r=48000", "-af", "volume=2.0",
           "-ac", "2", str(music))
    # timeline: intro 1.0 | seg0 1-5 | seg1 5-9 | seg2 9-12 | outro; the drop is at 7.0
    cue = {"time_s": 7.0, "on_drop": True, "bpm": 120}

    def film(name: str, editing: dict) -> list[float]:
        cfg = {
            "effects": {"intro_hook": False},
            "editing": {"intro_seconds": 1.0, "outro_seconds": 1.0, "sfx": False,
                        "sidechain_duck": False, "sfx_dir": str(tmp / "sfx"), **editing},
        }
        out = assemble(plan("a", "b", "c"), segs, music, tmp, cfg, log, hook_files=[],
                       cut_log=[], drop_cue=cue, final_name=name)
        return samples(out)

    plain = film("plain.mp4", {})
    cued = film("cued.mp4", dict(ALL_ON))
    # gap 6.9–7.0 (ramps 6.89–6.90 and 6.996–7.0); riser 2.9–6.9
    check("without cues the music plays through the gap window", rms_db(plain, 6.93, 6.97) > -30,
          f"{rms_db(plain, 6.93, 6.97):.1f} dB")
    check("with cues the gap is silent", rms_db(cued, 6.93, 6.97) < -60,
          f"{rms_db(cued, 6.93, 6.97):.1f} dB")
    lift = rms_db(cued, 6.5, 6.85) - rms_db(plain, 6.5, 6.85)
    check("the riser lifts the level before the gap by 6 dB or more", lift >= 6, f"+{lift:.1f} dB")
    check("before the riser the mix is unchanged",
          abs(rms_db(cued, 1.5, 2.5) - rms_db(plain, 1.5, 2.5)) < 0.5)
    lo_plain = samples(tmp / "plain.mp4", "lowpass=f=120,lowpass=f=120")
    lo_cued = samples(tmp / "cued.mp4", "lowpass=f=120,lowpass=f=120")
    bass = rms_db(lo_cued, 7.0, 7.3) - rms_db(lo_plain, 7.0, 7.3)
    check("the boom adds bass on the drop (15 dB or more)", bass >= 15, f"+{bass:.1f} dB")
    check("the film is as long as without cues",
          abs(probe_duration(tmp / "cued.mp4") - probe_duration(tmp / "plain.mp4")) < 0.05)
    check("nothing clips", max(abs(x) for x in cued) < 1.0)

    clean = film("clean.mp4", apply_style({"editing": dict(ALL_ON)}, "clean")["editing"])
    check("the clean style has no cues", rms_db(clean, 6.93, 6.97) > -30)


def test_presets() -> None:
    print("presets")
    for style, want in (("hype", (True, True, True)), ("cinematic", (True, True, False)),
                        ("clean", (False, False, False))):
        e = apply_style({}, style)["editing"]
        got = (e.get("riser"), e.get("drop_gap"), e.get("drop_boom"))
        check(f"{style}: riser/gap/boom = {want}", got == want, str(got))
    check("custom changes nothing", "riser" not in apply_style({}, "custom").get("editing", {}))


KEEP = ""  # a folder path here keeps the test films, for listening


if __name__ == "__main__":
    test_times()
    test_anchor()
    test_presets()
    with tempfile.TemporaryDirectory() as t:
        test_synth(Path(t))
    if KEEP:
        Path(KEEP).mkdir(parents=True, exist_ok=True)
        test_mix(Path(KEEP))
    else:
        with tempfile.TemporaryDirectory() as t:
            test_mix(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All sound design tests OK")
