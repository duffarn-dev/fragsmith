"""Tests for bullet time and the cinematic approach (the matrix style).

Covers: the worker writing a clip's camera path and putting it in the cfg
(schema 1.5), the render job (the bullet-time clip at 480 fps around the
climax kill with its path, no victim angle, the third-person approach on
the climax clip; the normal replay when there's no path), the replay plan
(bullet time when its clip is there, the normal replay otherwise), the
replay on a climax of any size, and a built bullet-time replay (its length,
slowed 4x, without the REPLAY badge).

Run: python tests/test_bullet_time.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import worker  # noqa: E402
from lib import render_clips  # noqa: E402
from lib.edit_engine import (  # noqa: E402
    BT_AFTER_S,
    BT_BEFORE_S,
    BT_FACTOR,
    BT_POST_S,
    BT_PRE_S,
    REPLAY_REWIND_S,
    _build_replay_segment,
    _replay_plan,
    pick_replay_clip,
)
from lib.ffmpeg_util import probe_duration  # noqa: E402
from lib.models import EditDecision, EditPlan, Highlight, KillMark  # noqa: E402
from lib.styles import apply_style  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def km(tick: int, start: int) -> KillMark:
    return KillMark(tick=tick, offset_s=(tick - start) / 64, weapon="ak47", headshot=True,
                    victim_name="bot", victim_steamid="765x")


def fixture():
    # the climax has ONE kill: bullet time goes on it anyway
    cands = {
        "a": Highlight(clip_id="a", start_tick=1000, end_tick=1640, player_steamid="765me",
                       kill_count=2, kind="multikill", hype_score=6.0, duration_seconds=10.0,
                       kills=[km(1300, 1000), km(1500, 1000)], player_name="T", round=2),
        "b": Highlight(clip_id="b", start_tick=5000, end_tick=6000, player_steamid="765me",
                       kill_count=1, kind="highlight_single", hype_score=7.0, duration_seconds=15.6,
                       kills=[km(5640, 5000)], player_name="T", round=9),
    }
    plan = EditPlan(demo_path="mock.dem", music_track="m.mp3", intro_text="X", outro_text="GG",
                    decisions=[EditDecision(clip_id=c, order=i + 1, slowmo_segments=[],
                                            overlay_text=None, transition_in="cut", reasoning="t",
                                            is_climax=c == "b")
                               for i, c in enumerate(cands)],
                    dropped_clip_ids=[])
    return cands, plan


def matrix_cfg() -> dict:
    return apply_style({"video": {"width": 1920, "height": 1080, "framerate": 60},
                        "render": {"highfps_framerate": 120}}, "matrix")


def test_worker(tmp: Path) -> None:
    print("the worker (schema 1.5)")
    job = {"run_id": "r", "pov_steamid": "765me", "video": {"width": 1920, "height": 1080, "framerate": 60},
           "clips": [{"clip_id": "b_bt", "number": 202, "start_tick": 1, "end_tick": 2, "framerate": 480,
                      "campath": "<campath/>", "cfg": 'mirv_campath load "{campath}"'},
                     {"clip_id": "a", "number": 1, "start_tick": 1, "end_tick": 2}]}
    seqs = {s["number"]: s for c in worker.build_csdm_configs(job, tmp / "d.dem", tmp / "out" / "_raw",
                                                                 tmp / "campaths")
            for s in c["sequences"]}
    path = tmp / "campaths" / "r_b_bt.xml"
    check("the camera path is written next to the job", path.read_text(encoding="utf-8") == "<campath/>")
    check("its path goes where the cfg says {campath}",
          seqs[202]["cfg"] == f'mirv_campath load "{path.as_posix()}"', seqs[202]["cfg"])
    check("a clip without a path is untouched", "cfg" not in seqs[1])


def test_render_job() -> None:
    print("the render job (the matrix style)")
    cands, plan = fixture()
    real = render_clips.bullet_time_campath
    render_clips.bullet_time_campath = lambda demo, steamid, tick: f"<campath kill='{tick}'/>"
    try:
        job = render_clips.build_render_job("run1", plan, cands, "765me", "T", matrix_cfg())
    finally:
        render_clips.bullet_time_campath = real
    by_id = {c["clip_id"]: c for c in job["clips"]}
    bt = by_id.get("b_bt")
    check("schema 1.5", job["schema_version"] == "1.5")
    check("a bullet-time clip for the climax kill", bt is not None)
    if bt:
        check("at 480 fps", bt["framerate"] == 480)
        check("around the kill", bt["start_tick"] == 5640 - round(BT_BEFORE_S * 64)
              and bt["end_tick"] == 5640 + round(BT_AFTER_S * 64), f"{bt['start_tick']}-{bt['end_tick']}")
        check("with its camera path, loaded through the cfg",
              bt["campath"] == "<campath kill='5640'/>" and '"{campath}"' in bt["cfg"]
              and "mirv_campath enabled 1" in bt["cfg"])
        check("its own sequence number", bt["number"] == 2 + render_clips.BT_SEQ_OFFSET)
    check("no victim angle: bullet time replaces it", "alt_pov_steamid" not in by_id["b"])
    cfg_b = by_id["b"].get("cfg", "")
    check("the climax clip: third person from its start, the player's eyes 1.5 s before the kill",
          'addAtTick 5008 "spec_mode 3"' in cfg_b and f'addAtTick {5640 - 96} "spec_mode 2"' in cfg_b, cfg_b)
    check("the other clips: no camera", "cfg" not in by_id["a"])

    render_clips.bullet_time_campath = lambda demo, steamid, tick: None
    try:
        job2 = render_clips.build_render_job("run1", plan, cands, "765me", "T", matrix_cfg())
    finally:
        render_clips.bullet_time_campath = real
    ids2 = {c["clip_id"]: c for c in job2["clips"]}
    check("no camera path: no bullet-time clip, the victim's angle instead",
          "b_bt" not in ids2 and ids2["b"].get("alt_pov_steamid") == "765x")


def test_replay_choice(tmp: Path) -> None:
    print("the replay")
    cands, plan = fixture()
    hype = apply_style({}, "hype")
    check("hype: no replay on a one-kill climax", pick_replay_clip(plan, cands, hype) is None)
    pick = pick_replay_clip(plan, cands, matrix_cfg())
    check("matrix: bullet time on the climax kill, whatever its size",
          pick is not None and pick[0] == "b" and pick[1].tick == 5640)
    clip = tmp / "b.mp4"
    rp = _replay_plan(tmp, "b", clip, 10.0, matrix_cfg())
    check("without its clip: the normal replay, and a note", rp["factor"] != BT_FACTOR and rp["note"])
    (tmp / "b_bt.mp4").write_bytes(b"")
    rp = _replay_plan(tmp, "b", clip, 10.0, matrix_cfg())
    check("with it: bullet time, the kill BT_BEFORE_S into it, at 0.25x, no badge",
          rp["src"].name == "b_bt.mp4" and rp["kill_s"] == BT_BEFORE_S
          and rp["factor"] == BT_FACTOR and rp["badge"] is None and rp["note"] is None)
    check("hype keeps its replay", _replay_plan(tmp, "b", clip, 10.0, hype)["badge"] == "REPLAY")


def test_build(tmp: Path) -> None:
    print("a built bullet-time replay")
    main = tmp / "main.mp4"
    bt = tmp / "b_bt.mp4"
    for path, rate, secs in ((main, 60, 4.0), (bt, 480, BT_BEFORE_S + BT_AFTER_S)):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"testsrc2=s=320x180:r={rate}:d={secs}", "-f", "lavfi", "-i",
                        f"sine=d={secs}", "-c:v", "libx264", "-preset", "ultrafast",
                        "-c:a", "aac", "-shortest", str(path)], check=True)
    out = tmp / "replay.mp4"
    got = _build_replay_segment(main, bt, BT_BEFORE_S, 3.0, 4.0, {"enabled": True}, out,
                                tmp / "log.txt", "test", pre_s=BT_PRE_S, post_s=BT_POST_S,
                                factor=BT_FACTOR, badge=None)
    want = REPLAY_REWIND_S + (BT_PRE_S + BT_POST_S) / BT_FACTOR
    check("rewind + the circling at 0.25x", abs(got - want) <= 0.1, f"{got:.2f} s, want {want:.2f}")
    log = (tmp / "log.txt").read_text(encoding="utf-8")
    check("no REPLAY badge", "text='REPLAY'" not in log)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as t:
        test_worker(Path(t))
    test_render_job()
    with tempfile.TemporaryDirectory() as t:
        test_replay_choice(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_build(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All bullet time tests OK")
