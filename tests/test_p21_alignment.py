"""P21-tester for drop-justeringen i edit_engine + cold open-tidslinjefiksen.

Mock-klipp i 720p/30fps (jf. tests/README.md) — motoren skal normalisere.
Kjør: python tests/test_p21_alignment.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.assemble import build_intro_hook  # noqa: E402
from lib.edit_engine import (  # noqa: E402
    _simulate_climax_start,
    build_segments,
    plan_drop_sync,
)
from lib.models import (  # noqa: E402
    EditDecision,
    EditPlan,
    Highlight,
    KillMark,
    SlowmoSegment,
)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FEIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_clip(path: Path, seconds: float) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:d={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def cfg_for_test() -> dict:
    """Som ekte config, men uten de trege effektene (minterpolate/LUT)."""
    return {
        "effects": {
            "enabled": True,
            "intensity": 1.0,
            "grade": True,
            "vignette": True,
            "beat_zoom": True,
            "flash_transitions": True,
            "kill_zoom": True,
            "kill_tilt": True,
            "intro_hook": True,
            "slowmo_punch": True,
            "eased_ramps": True,
            "smooth_slowmo": False,
            "letterbox_on_slowmo": True,
            "lut": "",
            "grain": 0.0,
            "freeze_on_finish": True,
            "headshot_punch": True,
            "headshot_micro_slowmo": True,
            "kill_counter": True,
            "lowerthird": True,
            "animated_overlays": True,
            "impact_split": True,
            "instant_replay": False,  # trenger _alt-klipp; ikke tema her
        },
        "editing": {
            "intro_seconds": 2.5,
            "outro_seconds": 2.5,
            "beat_tolerance_seconds": 0.75,
            "drop_sync": True,
        },
    }


def build_fixture(tmp: Path):
    """3 klipp: to vanlige + klimaks (sist). Kills gir zoom/punch/micro-slowmo
    så simuleringen må regne riktig gjennom hele tidslogikken."""
    clips = tmp / "clips"
    clips.mkdir()
    make_clip(clips / "c1.mp4", 8.0)
    make_clip(clips / "c2.mp4", 8.0)
    make_clip(clips / "c3.mp4", 10.0)

    def km(offset: float, hs: bool = False) -> KillMark:
        return KillMark(
            tick=int(offset * 64), offset_s=offset, weapon="ak47",
            headshot=hs, victim_name="bot", victim_steamid="765x",
        )

    def hl(cid: str, dur: float, kills: list[KillMark], **kw) -> Highlight:
        return Highlight(
            clip_id=cid, start_tick=0, end_tick=int(dur * 64),
            player_steamid="765me", kill_count=len(kills),
            kind=kw.pop("kind", "multikill"), hype_score=7.5,
            duration_seconds=dur, kills=kills, player_name="Testern",
            round=5, **kw,
        )

    candidates = {
        "c1": hl("c1", 8.0, [km(3.0, hs=True), km(6.0)]),
        "c2": hl("c2", 8.0, [km(2.5), km(5.5, hs=True)]),
        "c3": hl("c3", 10.0, [km(3.0), km(5.0, hs=True), km(7.5)]),
    }
    plan = EditPlan(
        demo_path="mock.dem", music_track="mock.mp3",
        intro_text="TESTERN", outro_text="GG",
        decisions=[
            EditDecision(
                clip_id="c1", order=1, slowmo_segments=[],
                overlay_text=None, transition_in="cut", reasoning="hook",
            ),
            EditDecision(
                clip_id="c2", order=2,
                slowmo_segments=[SlowmoSegment(start_s=5.0, end_s=6.0, factor=0.4)],
                overlay_text="2K", transition_in="cut", reasoning="midt",
            ),
            EditDecision(
                clip_id="c3", order=3, slowmo_segments=[],
                overlay_text="3K", transition_in="speed_ramp",
                reasoning="klimaks", is_climax=True,
            ),
        ],
        dropped_clip_ids=[],
    )
    return clips, candidates, plan


def beats_grid(drop_times: list[float], bpm: float = 120.0) -> dict:
    period = 60.0 / bpm
    n = int(180.0 / period)
    return {
        "bpm": bpm,
        "beat_times": [round(i * period, 3) for i in range(1, n)],
        "drops": [
            {"time_s": t, "strength": 1.0 - 0.1 * i, "ratio": 2.0}
            for i, t in enumerate(drop_times)
        ],
    }


def actual_climax_start(cut_log: list[dict], clip_id: str) -> float | None:
    entry = next((c for c in cut_log if c["clip_id"] == clip_id), None)
    if not entry:
        return None
    return entry["timeline_end_s"] - entry["segment_duration_s"]


def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        clips, candidates, plan = build_fixture(tmp)
        cfg = cfg_for_test()
        log = tmp / "ffmpeg.log"

        # -- cold open: glimtene som beat-synken nå må regne med -----------
        print("cold open (P14) på tidslinja:")
        hooks = build_intro_hook(candidates and plan, candidates, clips,
                                 tmp, cfg, log)
        from lib.ffmpeg_util import probe_duration
        hook_s = sum(probe_duration(h) for h in hooks)
        check("3 glimt bygges", len(hooks) == 3, f"{len(hooks)}")
        check("samlet glimt-lengde ~0.75 s", 0.6 <= hook_s <= 1.0,
              f"{hook_s:.3f} s")

        # -- naturlig klimaks-start (uten drop-justering) --------------------
        base = beats_grid([])
        c0 = _simulate_climax_start(
            plan, candidates, clips, base, cfg, hook_s + 2.5, {}
        )
        check("simulering finner klimaks-start", c0 is not None)
        print(f"    naturlig klimaks-start: {c0:.2f} s")

        # -- spak 1: drop nær naturlig klimaks → intro-justering -------------
        print("plan_drop_sync, spak 1 (intro):")
        drop_a = round(round((c0 + 1.5) / 0.5) * 0.5, 3)  # på et beat
        ds = plan_drop_sync(plan, candidates, clips,
                            beats_grid([drop_a]), cfg, hook_s)
        check("finner løsning", ds is not None)
        if ds:
            check("metode er intro-justering", ds["method"] == "intro",
                  ds["method"])
            check("intro innenfor grensene", 1.5 <= ds["intro_seconds"] <= 6.5,
                  f"{ds['intro_seconds']} s")
            check("predikert avvik <= 100 ms",
                  abs(ds["predicted_error_ms"]) <= 100,
                  f"{ds['predicted_error_ms']} ms")
            check("klimaks-id riktig", ds["climax_clip_id"] == "c3")

            # integrasjon: bygg segmentene og mål FAKTISK posisjon
            segs, cut_log, flags = build_segments(
                plan, candidates, clips, tmp / "seg_a", ds["beats"], cfg, log,
                context={"map": "de_test"},
                timeline_start=hook_s + ds["intro_seconds"],
            )
            start = actual_climax_start(cut_log, "c3")
            check("faktisk klimaks-start på droppet (±150 ms)",
                  start is not None and abs(start - ds["drop_timeline_s"]) <= 0.150,
                  f"start {start:.3f} s vs drop {ds['drop_timeline_s']} s")
            check("simulering vs faktisk bygging samsvarer (±150 ms)",
                  start is not None and abs(start - drop_a
                        - ds["predicted_error_ms"] / 1000) <= 0.150)

        # -- spak 2: drop langt ute → musikk-offset ---------------------------
        print("plan_drop_sync, spak 2 (musikk-offset):")
        drop_b = round(round((c0 + 20.0) / 0.5) * 0.5, 3)
        ds2 = plan_drop_sync(plan, candidates, clips,
                             beats_grid([drop_b]), cfg, hook_s)
        check("finner løsning", ds2 is not None)
        if ds2:
            check("metode er offset", ds2["method"] == "offset", ds2["method"])
            check("offset ~20 s", 18.0 <= ds2["music_offset"] <= 22.0,
                  f"{ds2['music_offset']} s")
            check("predikert avvik <= 100 ms",
                  abs(ds2["predicted_error_ms"]) <= 100,
                  f"{ds2['predicted_error_ms']} ms")
            check("beat-grid er forskjøvet",
                  ds2["beats"].get("music_offset_s") == ds2["music_offset"])

            segs, cut_log2, _ = build_segments(
                plan, candidates, clips, tmp / "seg_b", ds2["beats"], cfg, log,
                context={"map": "de_test"},
                timeline_start=hook_s + ds2["intro_seconds"],
            )
            start2 = actual_climax_start(cut_log2, "c3")
            check("faktisk klimaks-start på droppet (±150 ms)",
                  start2 is not None
                  and abs(start2 - ds2["drop_timeline_s"]) <= 0.150,
                  f"start {start2:.3f} s vs drop {ds2['drop_timeline_s']} s")

        # -- fallback: ingen drops / drop_sync av ----------------------------
        print("fallback:")
        check("ingen drops → None",
              plan_drop_sync(plan, candidates, clips, beats_grid([]),
                             cfg, hook_s) is None)
        cfg_off = {**cfg, "editing": {**cfg["editing"], "drop_sync": False}}
        check("drop_sync=false → None",
              plan_drop_sync(plan, candidates, clips, beats_grid([drop_a]),
                             cfg_off, hook_s) is None)
        # drop som ikke kan nås (før filmens start) → None
        check("uoppnåelig drop → None",
              plan_drop_sync(plan, candidates, clips, beats_grid([2.0]),
                             cfg, hook_s) is None)

    print()
    if FAILS:
        print(f"FEILET: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("Alle P21-justeringstester OK")


if __name__ == "__main__":
    main()
