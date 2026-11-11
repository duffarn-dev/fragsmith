"""Tests for lib/teaser.py: the teaser next to the film (finish --teaser).

Covers: the cut lengths (they fill the time, speed up, stay within limits),
which kills it uses (the climax clip's last as the hero, close kills in one
clip as one cut, nothing twice; from several runs, the best climax), and
rendered teasers (their parts, the drop about in the middle, the music's
drop where the hero kill is, more runs making a longer one).

Run: python tests/test_teaser.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np  # noqa: E402

from lib.ffmpeg_util import probe_duration  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402
from lib.teaser import PIECE_MAX_S, PIECE_MIN_S, _kills, build_teaser, cut_lengths  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_clip(path: Path, seconds: float) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                    f"testsrc2=s=320x180:r=60:d={seconds}", "-f", "lavfi", "-i",
                    f"sine=f=300:d={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True)


def km(offset: float, hs: bool = False) -> KillMark:
    return KillMark(tick=int(offset * 64), offset_s=offset, weapon="ak47", headshot=hs,
                    victim_name="bot", victim_steamid="765x")


def hl(cid: str, score: float, kills: list[KillMark]) -> Highlight:
    return Highlight(clip_id=cid, start_tick=0, end_tick=640, player_steamid="765me",
                     kill_count=len(kills), kind="multikill", hype_score=score,
                     duration_seconds=10.0, kills=kills, player_name="Tester", round=5)


def fixture(tmp: Path):
    clips = tmp / "clips"
    clips.mkdir()
    cands = {
        "a": hl("a", 5.0, [km(2.0), km(3.0, hs=True), km(7.5)]),  # 2.0 + 3.0 are one cut
        "b": hl("b", 6.0, [km(4.0), km(8.0)]),
        "c": hl("c", 8.0, [km(3.0), km(5.0)]),                     # the climax: 5.0 is the hero
    }
    for cid in cands:
        make_clip(clips / f"{cid}.mp4", 10.0)
    plan = EditPlan(demo_path="d.dem", music_track="m.wav", intro_text="X", outro_text="GG",
                    decisions=[EditDecision(clip_id=cid, order=i + 1, slowmo_segments=[],
                                            overlay_text=None, transition_in="cut", reasoning="t",
                                            is_climax=cid == "c")
                               for i, cid in enumerate(cands)],
                    dropped_clip_ids=[])
    return clips, cands, plan


def test_lengths() -> None:
    print("the cut lengths")
    cuts = cut_lengths(5, 6.0, 1.6, 0.6)
    check("they fill the time", abs(sum(cuts) - 6.0) < 1e-6, f"{sum(cuts):.2f}")
    check("each shorter than the last: they speed up", all(a > b for a, b in zip(cuts, cuts[1:])))
    few = cut_lengths(2, 20.0, 1.6, 0.6)
    check("two cuts can't fill 20 s: capped", few == [PIECE_MAX_S, PIECE_MAX_S])
    many = cut_lengths(20, 3.0, 1.6, 0.6)
    check("never shorter than the minimum", min(many) >= PIECE_MIN_S)
    check("no cuts, no lengths", cut_lengths(0, 5.0, 1.6, 0.6) == [])


def test_kills(tmp: Path) -> None:
    print("the kills it uses")
    clips, cands, plan = fixture(tmp)
    hero, others = _kills([(plan, cands, clips)])
    check("the hero is the climax clip's last kill", hero[1].stem == "c" and hero[2] == 5.0)
    check("a kill just before the hero is in its rewind, not a cut of its own",
          all(not (c.stem == "c" and o == 3.0) for _, c, o in others))
    a_cuts = sorted(o for _, c, o in others if c.stem == "a")
    check("close kills in one clip are one cut (on the last)", a_cuts == [3.0, 7.5], str(a_cuts))
    check("rising: the strongest last", [p[0] for p in others] == sorted(p[0] for p in others))


def test_render(tmp: Path) -> None:
    print("a rendered teaser")
    clips, cands, plan = fixture(tmp)
    wav = music(tmp)
    cfg = {"editing": {"sfx_dir": str(tmp / "sfx")}, "branding": {}}
    path, info = build_teaser([(plan, cands, clips)], wav, 40.0, tmp, cfg, tmp / "log.txt")
    check_render(path, info, kills=5)
    return info


def music(tmp: Path) -> Path:
    """A quiet track that jumps 20 dB at 40 s: its drop."""
    # a quiet track that jumps 20 dB at 40 s: its drop
    sr = 48000
    t = np.arange(int(60 * sr)) / sr
    music = np.sin(2 * np.pi * 220 * t) * np.where(t < 40.0, 0.03, 0.3)
    wav = tmp / "m.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((music * 32767).astype(np.int16).tobytes())
    return wav


def check_render(path: Path, info: dict, kills: int) -> None:
    check("teaser.mp4 in the folder", path.name == "teaser.mp4" and path.exists())
    check("as long as it says", abs(probe_duration(path) - info["seconds"]) <= 0.1,
          f"{probe_duration(path):.2f} vs {info['seconds']}")
    check("the drop about in the middle", abs(info["drop_s"] - info["seconds"] / 2) <= 1.5,
          f"drop at {info['drop_s']} of {info['seconds']} s")
    check(f"every kill moment once: {kills - 1} cuts and the hero", info["kills"] == kills, str(info["kills"]))
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", "8000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    y = np.frombuffer(raw, dtype=np.float32)

    def level(a: float, b: float) -> float:
        x = y[int(a * 8000):int(b * 8000)]
        return 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)

    before, after = level(info["drop_s"] - 0.6, info["drop_s"] - 0.1), level(info["drop_s"] + 0.1, info["drop_s"] + 0.6)
    check("the music's drop is where the hero kill is", after - before > 10,
          f"{before:.1f} dB before, {after:.1f} dB after")


def test_many(tmp: Path, one_run: dict) -> None:
    print("best of the week: several runs")
    (tmp / "r1").mkdir()
    (tmp / "r2").mkdir()
    c1, k1, p1 = fixture(tmp / "r1")
    c2, k2, p2 = fixture(tmp / "r2")
    k2["c"] = k2["c"].model_copy(update={"hype_score": 9.0})  # the better climax
    hero, others = _kills([(p1, k1, c1), (p2, k2, c2)])
    check("the hero is the best climax of all the runs", hero[1].parent.parent.name == "r2")
    check("the other run's climax kill is one of the cuts",
          any(c.parent.parent.name == "r1" and c.stem == "c" and o == 5.0 for _, c, o in others))
    cfg = {"editing": {"sfx_dir": str(tmp / "sfx")}, "branding": {}}
    path, info = build_teaser([(p1, k1, c1), (p2, k2, c2)], music(tmp), 40.0, tmp, cfg, tmp / "log.txt")
    check_render(path, info, kills=10)
    check("more runs, a longer teaser", info["seconds"] > one_run["seconds"] + 5,
          f"{info['seconds']} s against {one_run['seconds']} s")


if __name__ == "__main__":
    test_lengths()
    with tempfile.TemporaryDirectory() as t:
        test_kills(Path(t))
    with tempfile.TemporaryDirectory() as t:
        one = test_render(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_many(Path(t), one)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All teaser tests OK")
