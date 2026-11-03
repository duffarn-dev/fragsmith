"""Tests for kill context: NOSCOPE / WALLBANG / THROUGH SMOKE / JUMPSHOT /
BLIND / long shots, from the demo's player_death event to the callout in the
film.

Covers: reading the event row (missing columns and NaN), the style points in
the hype score (and the cap), which single kills get a clip of their own, the
callout label, old candidate files, and the drawtext in a real segment.

Run: python tests/test_kill_tags.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.edit_engine import build_segments, kill_tag  # noqa: E402
from lib.hype_score import _score_cluster, style_points  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402
from lib.parse_demo import kill_context  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def kill(tick: int, weapon: str = "ak47", hs: bool = False, **ctx) -> dict:
    return {"tick": tick, "weapon": weapon, "headshot": hs, "victim_name": "v",
            "attacker_name": "me", **ctx}


def test_event_row() -> None:
    print("reading the event row")
    nan = float("nan")
    ctx = kill_context({"noscope": True, "thrusmoke": False, "penetrated": 2,
                        "attackerinair": nan, "distance": 41.66})
    check("flags, wallbang from the penetration count, NaN is False",
          ctx["noscope"] and ctx["wallbang"] and not ctx["through_smoke"] and not ctx["jumpshot"], str(ctx))
    check("distance rounded to 0.1 m", ctx["distance_m"] == 41.7)
    check("missing columns (older demos) give nothing", kill_context({}) == {
        "noscope": False, "through_smoke": False, "wallbang": False, "jumpshot": False,
        "blind": False, "distance_m": 0.0})


def test_scoring() -> None:
    print("style points in the hype score")
    plain = _score_cluster([kill(0), kill(64)], None, 64)
    styled = _score_cluster([kill(0, through_smoke=True), kill(64, wallbang=True)], None, 64)
    check("a 2K with two style kills scores 1.0 more", round(styled.hype_score - plain.hype_score, 2) == 1.0,
          f"{plain.hype_score} vs {styled.hype_score}")
    loaded = _score_cluster([kill(0, noscope=True, wallbang=True, through_smoke=True, jumpshot=True)] * 2, None, 64)
    check("the bonus is capped at +1.5", round(loaded.hype_score - plain.hype_score, 2) == 1.5)
    check("a long shot is a style point", style_points(kill(0, distance_m=40.0)) == 1
          and style_points(kill(0, distance_m=20.0)) == 0)

    single = lambda **kw: _score_cluster([kill(0, **kw)], None, 64)  # noqa: E731
    check("a plain single kill gets no clip", single() is None)
    check("a noscope gets a clip on its own", single(noscope=True) is not None)
    check("a jumpshot gets a clip on its own", single(jumpshot=True) is not None)
    check("through smoke needs a headshot to stand alone",
          single(through_smoke=True) is None and single(through_smoke=True, hs=True) is not None)
    check("a wallbang needs a headshot to stand alone",
          single(wallbang=True) is None and single(wallbang=True, hs=True) is not None)


def km(offset: float, **ctx) -> KillMark:
    return KillMark(tick=int(offset * 64), offset_s=offset, weapon="ak47", headshot=True,
                    victim_name="v", **ctx)


def test_label() -> None:
    print("the callout")
    check("none for a plain kill", kill_tag(km(1.0)) is None)
    check("one detail", kill_tag(km(1.0, through_smoke=True)) == "THROUGH SMOKE")
    check("the two most special, in order",
          kill_tag(km(1.0, blind=True, noscope=True, wallbang=True)) == "NOSCOPE  ·  WALLBANG")
    check("a long shot in meters", kill_tag(km(1.0, distance_m=42.4)) == "42 M")
    old = Highlight.model_validate({
        "clip_id": "x", "start_tick": 0, "end_tick": 640, "player_steamid": "7656119",
        "kill_count": 1, "kind": "multikill", "hype_score": 1.0,
        "kills": [{"tick": 64, "offset_s": 1.0, "weapon": "ak47", "headshot": True, "victim_name": "v"}],
    })
    check("candidate files from before still load, with no tags", kill_tag(old.kills[0]) is None)


def test_segment(tmp: Path) -> None:
    print("the callout in a segment")
    clips = tmp / "clips"
    clips.mkdir()
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:d=5",
                    "-f", "lavfi", "-i", "sine=f=440:d=5", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clips / "x.mp4")], check=True)
    cand = Highlight(clip_id="x", start_tick=0, end_tick=320, player_steamid="765me", kill_count=2,
                     kind="multikill", hype_score=5.0, duration_seconds=5.0,
                     kills=[km(1.5, through_smoke=True), km(3.0)])
    plan = EditPlan(demo_path="m.dem", music_track="m.mp3", intro_text="", outro_text="",
                    decisions=[EditDecision(clip_id="x", order=1, slowmo_segments=[], overlay_text=None,
                                            transition_in="cut", reasoning="t")],
                    dropped_clip_ids=[])
    beats = {"bpm": 120.0, "beat_times": [], "drops": []}
    for on in (True, False):
        log = tmp / f"ffmpeg_{on}.log"
        cfg = {"effects": {"enabled": True, "kill_tags": on, "kill_zoom": False, "grain": 0.0, "lut": ""},
               "editing": {"beat_tolerance_seconds": 0.0, "intro_seconds": 0.0}}
        build_segments(plan, {"x": cand}, clips, tmp / f"seg_{on}", beats, cfg, log, timeline_start=0.0)
        text = log.read_text(encoding="utf-8")
        if on:
            check("the callout is drawn at the kill (1.5 s)",
                  "text='THROUGH SMOKE'" in text and "between(t,1.500,2.600)" in text)
        else:
            check("kill_tags = false: no callout", "THROUGH SMOKE" not in text)


if __name__ == "__main__":
    test_event_row()
    test_scoring()
    test_label()
    with tempfile.TemporaryDirectory() as t:
        test_segment(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All kill tag tests OK")
