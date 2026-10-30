"""P20-tester: høy-fps-utvalg, jobbschema 1.3, worker-pass og ekte slow-mo.

Kjør: python tests/test_p20.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import worker  # noqa: E402 — stdlib-only, importerbar fra repo-rota
from lib.edit_engine import (  # noqa: E402
    _timing_plan,
    build_segments,
    source_to_output_time,
    wants_high_fps,
)
from lib.ffmpeg_util import probe_fps  # noqa: E402
from lib.models import (  # noqa: E402
    EditDecision,
    EditPlan,
    Highlight,
    KillMark,
    SlowmoSegment,
)
from lib.render_clips import build_render_job  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FEIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def km(offset: float, hs: bool = False) -> KillMark:
    return KillMark(
        tick=int(offset * 64), offset_s=offset, weapon="ak47",
        headshot=hs, victim_name="bot", victim_steamid="765x",
    )


def hl(cid: str, dur: float, kills: list[KillMark], **kw) -> Highlight:
    return Highlight(
        clip_id=cid, start_tick=0, end_tick=int(dur * 64),
        player_steamid="765me", kill_count=len(kills),
        kind=kw.pop("kind", "multikill"), hype_score=7.0,
        duration_seconds=dur, kills=kills, player_name="Testern", **kw,
    )


def dec(cid: str, order: int, slowmo=None, **kw) -> EditDecision:
    return EditDecision(
        clip_id=cid, order=order, slowmo_segments=slowmo or [],
        overlay_text=None, transition_in="cut", reasoning="test", **kw,
    )


FX = {
    "enabled": True, "intensity": 1.0,
    "kill_zoom": True, "headshot_punch": True, "headshot_micro_slowmo": True,
    "eased_ramps": True, "freeze_on_finish": False, "instant_replay": True,
}


def test_wants_high_fps() -> None:
    print("wants_high_fps:")
    slow = [SlowmoSegment(start_s=2.0, end_s=3.0, factor=0.5)]
    c_plain = hl("a", 8.0, [km(3.0), km(6.0)])
    # headshot midt i klippet (ikke siste kill → dekkes ikke av kill_zoom)
    c_hs = hl("b", 8.0, [km(3.0, hs=True), km(6.0)])

    check("Director-slowmo → høy fps", wants_high_fps(dec("a", 1, slow), c_plain, FX))
    check("headshot-mikro → høy fps", wants_high_fps(dec("b", 1), c_hs, FX))
    check("ingen slow-mo → base fps", not wants_high_fps(dec("a", 1), c_plain, FX))
    fx_off = {**FX, "headshot_punch": False}
    check(
        "mikro av i config → base fps",
        not wants_high_fps(dec("b", 1), c_hs, fx_off),
    )
    check(
        "Director effects_off headshot_punch → base fps",
        not wants_high_fps(dec("b", 1, effects_off=["headshot_punch"]), c_hs, FX),
    )


def make_cfg(high: int = 120) -> dict:
    return {
        "video": {"width": 1920, "height": 1080, "framerate": 60},
        "render": {"highfps_framerate": high, "true_view": True},
        "effects": FX,
    }


def build_job(high: int = 120) -> dict:
    slow = [SlowmoSegment(start_s=2.0, end_s=3.0, factor=0.5)]
    cands = {
        "a": hl("a", 8.0, [km(3.0), km(6.0)]),
        # klimaks med 3 kills + clutch → replay-pick
        "b": hl("b", 10.0, [km(3.0), km(5.0), km(7.5)], kind="clutch",
                clutch_vs=3),
        "c": hl("c", 8.0, [km(4.0)]),
    }
    plan = EditPlan(
        demo_path="mock.dem", music_track="m.mp3", intro_text="X",
        outro_text="GG",
        decisions=[dec("c", 1), dec("a", 2, slow), dec("b", 3, is_climax=True)],
        dropped_clip_ids=[],
    )
    return build_render_job("run1", plan, cands, "765me", "Testern",
                            make_cfg(high))


def test_build_render_job() -> None:
    print("build_render_job (schema 1.3):")
    job = build_job()
    by_id = {c["clip_id"]: c for c in job["clips"]}
    check("schema 1.3", job["schema_version"] == "1.3")
    check("slow-mo-klipp får framerate", by_id["a"].get("framerate") == 120)
    check("replay-klimaks får framerate", by_id["b"].get("framerate") == 120)
    check("vanlig klipp får IKKE framerate", "framerate" not in by_id["c"])
    check("replay-klippet har alt-POV", by_id["b"].get("alt_pov_steamid") == "765x")

    job_off = build_job(high=0)
    check(
        "highfps 0 → ingen framerate-felt",
        all("framerate" not in c for c in job_off["clips"]),
    )
    job_low = build_job(high=60)
    check(
        "highfps <= base → ingen framerate-felt",
        all("framerate" not in c for c in job_low["clips"]),
    )


def test_worker_configs() -> None:
    print("worker.build_csdm_configs:")
    job = build_job()
    with tempfile.TemporaryDirectory() as tmp:
        configs = worker.build_csdm_configs(job, Path(tmp) / "d.dem", Path(tmp))
    check("2 pass ved blandet fps", len(configs) == 2, f"{len(configs)}")
    base, high = configs
    check("base-passet først (60 fps)", base["framerate"] == 60)
    check("høy-fps-passet sist (120 fps)", high["framerate"] == 120)
    base_nums = {s["number"] for s in base["sequences"]}
    high_nums = {s["number"] for s in high["sequences"]}
    check("base har kun vanlig klipp", base_nums == {1})
    # a=order 2, b=order 3 + alt-sekvens 103
    check("høy-fps har slow-mo + replay + alt", high_nums == {2, 3, 103},
          str(sorted(high_nums)))
    check(
        "alt-sekvensen arver klippets fps og peker på offeret",
        next(s for s in high["sequences"] if s["number"] == 103)
        ["playerCameras"][0]["playerSteamId"] == "765x",
    )
    check(
        "felles innstillinger intakte i begge pass",
        all(c["ffmpegSettings"]["videoContainer"] == "mp4"
            and c["recordingSystem"] == "HLAE" and c["trueView"]
            for c in configs),
    )

    # bakoverkompatibilitet: jobb uten framerate-felt → ett pass som før
    legacy = build_job(high=0)
    with tempfile.TemporaryDirectory() as tmp:
        configs = worker.build_csdm_configs(legacy, Path(tmp) / "d.dem", Path(tmp))
    check("legacy-jobb → 1 pass @ 60", len(configs) == 1
          and configs[0]["framerate"] == 60)
    check("legacy-passet har alle sekvenser (inkl. alt)",
          {s["number"] for s in configs[0]["sequences"]} == {1, 2, 3, 103})


def make_clip(path: Path, seconds: float, rate: int) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i",
            f"testsrc2=size=1280x720:rate={rate}:d={seconds}",
            "-f", "lavfi", "-i", f"sine=f=440:d={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def dup_ratio(video: Path, t0: float, t1: float) -> float:
    """Andel etterfølgende frame-par med identisk innhold (framemd5)."""
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}",
         "-i", str(video), "-map", "0:v", "-f", "framemd5", "-"],
        capture_output=True, text=True, check=True,
    )
    hashes = [
        line.rsplit(",", 1)[-1].strip()
        for line in proc.stdout.splitlines()
        if line and not line.startswith("#")
    ]
    dups = sum(1 for a, b in zip(hashes, hashes[1:]) if a == b)
    return dups / max(1, len(hashes) - 1)


def test_real_slowmo() -> None:
    print("ekte slow-mo fra høy-fps-kilde (steg 6):")
    # kun det som trengs: smooth_slowmo på, alt annet av → ren filterkjede
    fx = {
        "enabled": True, "intensity": 0.0, "grade": False, "vignette": False,
        "beat_zoom": False, "flash_transitions": False, "kill_zoom": False,
        "headshot_punch": False, "eased_ramps": False, "slowmo_punch": False,
        "smooth_slowmo": True, "freeze_on_finish": False, "grain": 0.0,
        "lut": "", "instant_replay": False,
    }
    cfg = {"effects": fx, "editing": {"beat_tolerance_seconds": 0.0,
                                      "intro_seconds": 0.0}}
    beats = {"bpm": 120.0, "beat_times": [], "drops": []}
    slow = [SlowmoSegment(start_s=2.0, end_s=3.0, factor=0.5)]
    cand = hl("x", 6.0, [])
    plan = EditPlan(
        demo_path="mock.dem", music_track="m.mp3", intro_text="X",
        outro_text="GG", decisions=[dec("x", 1, slow)], dropped_clip_ids=[],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        results = {}
        for rate in (60, 120):
            clips = tmp / f"clips{rate}"
            clips.mkdir()
            make_clip(clips / "x.mp4", 6.0, rate)
            check(f"mock er {rate} fps", abs(probe_fps(clips / "x.mp4") - rate) < 1)
            segs, cut_log, _ = build_segments(
                plan, {"x": cand}, clips, tmp / f"seg{rate}", beats, cfg,
                tmp / "ffmpeg.log", timeline_start=0.0,
            )
            # slow-mo-vinduet i output-tid, via samme parts som motoren
            parts = _timing_plan(plan.decisions[0], cand, 6.0, fx)["parts"]
            w0 = source_to_output_time(2.2, parts)
            w1 = source_to_output_time(2.8, parts)
            results[rate] = dup_ratio(segs[0], w0 + 0.05, w1 - 0.05)

        log = (tmp / "ffmpeg.log").read_text(encoding="utf-8")
        cmds60 = [l for l in log.splitlines() if "clips60" in l and "cmd:" in l]
        cmds120 = [l for l in log.splitlines() if "clips120" in l and "cmd:" in l]
        check("60 fps-kilde bruker minterpolate (P8 som før)",
              any("minterpolate" in l for l in cmds60))
        check("120 fps-kilde bruker IKKE minterpolate (P20)",
              not any("minterpolate" in l for l in cmds120))
        check(
            "120 fps-kilde gir unike frames i slow-mo (< 5 % duplikater)",
            results[120] < 0.05, f"{results[120]:.0%} duplikater",
        )
        print(f"    duplikat-andel i slow-mo-vinduet: "
              f"60fps-kilde {results[60]:.0%} (interpolert), "
              f"120fps-kilde {results[120]:.0%} (ekte frames)")


if __name__ == "__main__":
    test_wants_high_fps()
    test_build_render_job()
    test_worker_configs()
    test_real_slowmo()
    print()
    if FAILS:
        print(f"FEILET: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("Alle P20-tester OK")
