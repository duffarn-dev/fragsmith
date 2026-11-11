"""Tests for the drop cues in lib/assemble.py: a riser into the drop, a gap of
silence just before it and a boom on it.

Covers: where the cues are anchored (the drop, else the climax clip), their
timing from the BPM, what turns them off, the synthesized riser and boom, and
the riser's level against the music under it (a loud, a quiet and a
near-silent build-up), and the real mix measured in time windows: a riser as
loud as the music before the gap, silence in it, and bass on the drop.

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
    RISER_MATCH_SFX_VOLUME,
    _ensure_sfx,
    _text_card,
    assemble,
    drop_cue,
    drop_cue_times,
    riser_gain,
)
from lib.ffmpeg_util import mean_volume, probe_duration  # noqa: E402
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


def without(total_db: float, other_db: float) -> float:
    """The level of one sound in a sum, given the sum and the other sound
    (uncorrelated, so their powers add)."""
    rest = 10 ** (total_db / 10) - 10 ** (other_db / 10)
    return 10 * math.log10(rest) if rest > 0 else -200.0


def tone(path: Path, volume: float, seconds: float = 20) -> Path:
    """A steady 440 Hz tone: -24.1 dB RMS at volume 1 (the sine source's
    default), -18.1 dB at 2."""
    ffmpeg("-f", "lavfi", "-i", f"sine=f=440:d={seconds}:r=48000", "-af", f"volume={volume}",
           "-ac", "2", str(path))
    return path


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


def test_riser_gain(tmp: Path) -> None:
    print("the riser's level follows the music")
    riser = _ensure_sfx(tmp / "sfx", tmp / "log.txt")["riser"]
    level = tone(tmp / "level.wav", 2.0, 4)
    check("mean_volume reads a known tone", abs(mean_volume(level) + 18.1) < 0.3,
          f"{mean_volume(level):.1f} dB")
    check("mean_volume reads only the stretch asked for",
          mean_volume(riser, 0, 0.5) < mean_volume(riser, 3.5, 0.5) - 20)
    end_db = mean_volume(riser, 3.5, 0.5)

    def ends_at(music: Path, vol: float) -> float:
        return end_db + 20 * math.log10(riser_gain(riser, music, 0, 4, vol))

    quiet = tone(tmp / "quiet.wav", 1.0, 4)  # -24 dB, a quiet build-up
    check("at hype's sfx_volume it ends as loud as the music",
          abs(ends_at(quiet, RISER_MATCH_SFX_VOLUME) - mean_volume(quiet)) < 0.2,
          f"{ends_at(quiet, RISER_MATCH_SFX_VOLUME):.1f} vs {mean_volume(quiet):.1f} dB")
    check("at sfx_volume 0.35 it ends 2.2 dB under the music",
          abs(ends_at(quiet, 0.35) - (mean_volume(quiet) - 2.2)) < 0.2,
          f"{ends_at(quiet, 0.35):.1f} dB")
    loud = tone(tmp / "loud.wav", 10.0, 4)  # -4 dB
    check("under loud music it's never louder than the old fixed level",
          riser_gain(riser, loud, 0, 4, 0.35) == 0.35 * 2.6)
    silent = tone(tmp / "silent.wav", 0.01, 4)  # -64 dB
    floor = tone(tmp / "floor.wav", 0.5, 4)  # -30 dB
    check("a near-silent build-up counts as -30 dB, so the riser is still heard",
          abs(ends_at(silent, 0.35) - ends_at(floor, 0.35)) < 0.3,
          f"{ends_at(silent, 0.35):.1f} vs {ends_at(floor, 0.35):.1f} dB")
    check("sfx_volume 0: silent", riser_gain(riser, quiet, 0, 4, 0.0) == 0.0)


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
    # constant music at -18 dB RMS, and a quiet one at -24 dB
    music = tone(tmp / "music.wav", 2.0)
    quiet = tone(tmp / "quiet-music.wav", 1.0)
    # timeline: intro 1.0 | seg0 1-5 | seg1 5-9 | seg2 9-12 | outro; the drop is at 7.0
    cue = {"time_s": 7.0, "on_drop": True, "bpm": 120}

    def film(name: str, editing: dict, track: Path = music) -> list[float]:
        cfg = {
            "effects": {"intro_hook": False},
            "editing": {"intro_seconds": 1.0, "outro_seconds": 1.0, "sfx": False,
                        "sidechain_duck": False, "sfx_dir": str(tmp / "sfx"), **editing},
        }
        out = assemble(plan("a", "b", "c"), segs, track, tmp, cfg, log, hook_files=[],
                       cut_log=[], drop_cue=cue, final_name=name)
        return samples(out)

    plain = film("plain.mp4", {})
    cued = film("cued.mp4", dict(ALL_ON))
    # gap 6.9–7.0 (ramps 6.89–6.90 and 6.996–7.0); riser 2.9–6.9
    check("without cues the music plays through the gap window", rms_db(plain, 6.93, 6.97) > -30,
          f"{rms_db(plain, 6.93, 6.97):.1f} dB")
    check("with cues the gap is silent", rms_db(cued, 6.93, 6.97) < -60,
          f"{rms_db(cued, 6.93, 6.97):.1f} dB")
    # the riser's last half second, without the music under it; sfx_volume
    # is the default 0.35, so it ends 2.2 dB under the music
    under = 20 * math.log10(0.35 / RISER_MATCH_SFX_VOLUME)
    end = without(rms_db(cued, 6.4, 6.85), rms_db(plain, 6.4, 6.85))
    check("the riser ends 2.2 dB under the music", abs(end - (rms_db(plain, 6.4, 6.85) + under)) < 1.5,
          f"{end:.1f} dB, music {rms_db(plain, 6.4, 6.85):.1f} dB")
    lift = rms_db(cued, 6.4, 6.85) - rms_db(plain, 6.4, 6.85)
    check("the riser is heard: it lifts the level before the gap", lift >= 1.5, f"+{lift:.1f} dB")
    # a quiet build-up: the old fixed level ended 12 dB over this one
    soft = film("quiet.mp4", dict(ALL_ON), quiet)
    soft_music = rms_db(soft, 1.5, 2.5)
    soft_end = without(rms_db(soft, 6.4, 6.85), soft_music)
    check("a quiet build-up gets a quiet riser", abs(soft_end - (soft_music + under)) < 1.5,
          f"{soft_end:.1f} dB, music {soft_music:.1f} dB")
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

    # a clip change at 5.0: the whoosh would run 4.7-5.3. Above 2 kHz the
    # 440 Hz music is gone, so the whoosh is all that's left
    sfx_only = film("sfx.mp4", {"sfx": True})
    check("sfx alone adds no whoosh on a clip change",
          abs(rms_db(sfx_only, 4.75, 5.25) - rms_db(plain, 4.75, 5.25)) < 0.5)
    hi_sfx = samples(tmp / "sfx.mp4", "highpass=f=2000,highpass=f=2000")
    film("whoosh.mp4", {"sfx": True, "whoosh": True})
    hi_whoosh = samples(tmp / "whoosh.mp4", "highpass=f=2000,highpass=f=2000")
    lift = rms_db(hi_whoosh, 4.75, 5.25) - rms_db(hi_sfx, 4.75, 5.25)
    check("whoosh = true adds it (20 dB or more above 2 kHz)", lift >= 20, f"+{lift:.1f} dB")


def test_presets() -> None:
    print("presets")
    # no style turns on a sound on the transitions: the riser and the whoosh
    # are yours to turn on, and a style leaves them as you set them
    for style, want in (("hype", (None, True, True)), ("cinematic", (None, True, False)),
                        ("clean", (False, False, False))):
        e = apply_style({}, style)["editing"]
        got = (e.get("riser"), e.get("drop_gap"), e.get("drop_boom"))
        check(f"{style}: riser/gap/boom = {want}", got == want, str(got))
    for style in ("hype", "cinematic"):
        e = apply_style({"editing": {"riser": True, "whoosh": True}}, style)["editing"]
        check(f"{style}: your riser and whoosh stay on",
              (e.get("riser"), e.get("whoosh")) == (True, True), str(e))
    check("custom changes nothing", "riser" not in apply_style({}, "custom").get("editing", {}))


KEEP = ""  # a folder path here keeps the test films, for listening


if __name__ == "__main__":
    test_times()
    test_anchor()
    test_presets()
    with tempfile.TemporaryDirectory() as t:
        test_synth(Path(t))
        test_riser_gain(Path(t))
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
