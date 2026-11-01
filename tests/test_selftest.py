"""Tests for lib/selftest.py and the version detection in worker.py.

The real selftest needs CS2 and a fresh demo; these cover its building blocks:
version comparison, demo and clip picking, the click track, the clip checks
(size, fps, black picture), recording tested versions, and an early stop.

Run: python tests/test_selftest.py
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import worker  # noqa: E402
from lib import selftest as st  # noqa: E402
from lib.doctor import Report  # noqa: E402
from lib.models import Highlight  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def quiet():
    return contextlib.redirect_stdout(io.StringIO())


def hl(cid: str, score: float, rnd: int) -> Highlight:
    return Highlight(clip_id=cid, start_tick=0, end_tick=640, player_steamid="76561190000000001",
                     kill_count=2, kind="multikill", hype_score=score, round=rnd)


def test_versions() -> None:
    print("versions")
    check("same", st.compare("2.192.10", "2.192.10") == "same")
    check("newer", st.compare("2.192.10", "2.192.9") == "newer")
    check("older", st.compare("0.41.3", "0.42.1") == "older")
    check("unknown", st.compare(None, "1.0") == "unknown" and st.compare("1.0", None) == "unknown")
    check("CS2 patch as demos store it", st.patch_digits("1.41.9.4") == "14194")

    with tempfile.TemporaryDirectory() as tmp:
        inf = Path(tmp, "game", "csgo", "steam.inf")
        inf.parent.mkdir(parents=True)
        inf.write_text("ClientVersion=2000939\nPatchVersion=1.41.9.4\nVersionDate=Oct 30 2026\n")
        data = worker.read_steam_inf(Path(tmp))
        check("steam.inf is read", (data.get("PatchVersion"), data.get("ClientVersion")) == ("1.41.9.4", "2000939"))
        check("missing steam.inf gives {}", worker.read_steam_inf(Path(tmp, "nope")) == {})
    if os.name == "nt":
        v = worker.file_version(Path(sys.executable))
        check("Windows file version of python.exe", bool(v) and v.count(".") == 3, str(v))
    check("no file version for a missing file", worker.file_version(Path("nope.exe")) is None)
    v = worker.detect_versions(Path("C:/nowhere/csdm.cmd"))
    check("detect_versions never fails and has every key",
          {"cs2", "hlae", "cs_demo_manager"} <= set(v) and v["cs_demo_manager"] is None, str(v))


def test_picking() -> None:
    print("picking the demo and the test clips")
    cands = [hl("a", 6.0, 19), hl("b", 3.1, 4), hl("c", 4.0, 22), hl("d", 2.0, 19)]
    check("best + weakest from another round", [c.clip_id for c in st.pick_test_clips(cands)] == ["b", "a"])
    check("one round only: just the best", [c.clip_id for c in st.pick_test_clips([hl("a", 6, 1), hl("b", 2, 1)])] == ["a"])

    with tempfile.TemporaryDirectory() as tmp:
        old, new = Path(tmp, "old.dem"), Path(tmp, "sub", "new.dem.bz2")
        new.parent.mkdir()
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        Path(tmp, "notes.txt").write_text("x")
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        cfg = {"selftest": {"demo_dir": tmp}}
        check("newest demo, subfolders included", st.pick_demo(None, cfg) == new)
        check("--demo wins", st.pick_demo("x.dem", cfg) == Path("x.dem"))
        check("no folder configured", st.pick_demo(None, {}) is None)


def test_both_render_passes() -> None:
    print("both render passes run")
    from lib.models import KillMark
    from lib.rules_director import run_rules_director

    def multikill(cid: str, score: float, rnd: int) -> Highlight:
        kills = [KillMark(tick=64 * (2 + 3 * i), offset_s=2.0 + 3 * i, weapon="ak47", headshot=True,
                          victim_name=f"v{i}", victim_steamid=f"7656119000000010{i}") for i in range(2)]
        return Highlight(clip_id=cid, start_tick=0, end_tick=640, player_steamid="76561190000000001",
                         kill_count=2, kind="multikill", hype_score=score, round=rnd,
                         duration_seconds=10.0, headshots=2, kills=kills)

    chosen = [multikill("weak", 3.0, 4), multikill("best", 6.0, 19)]
    cfg = {"video": {"framerate": 60}, "render": {"highfps_framerate": 120},
           "effects": {"enabled": True, "headshot_punch": True, "headshot_micro_slowmo": True,
                       "instant_replay": True}}
    plan = run_rules_director(chosen, {"demo_path": "x.dem"}, ["click.wav"], "pov")
    by_id = {c.clip_id: c for c in chosen}
    before = st.expected_fps(plan, by_id, cfg)
    st.first_clip_at_base_fps(plan)
    after = st.expected_fps(plan, by_id, cfg)
    check("headshot micro slow-mo makes both clips high fps", before.get("weak") == 120, str(before))
    check("after the fix: the opener at 60, the climax at 120",
          after.get("weak") == 60 and after.get("best") == 120, str(after))


def test_media(tmp: Path) -> None:
    print("click track and clip checks")
    click = tmp / "click.wav"
    st.make_click_track(click, seconds=20)
    from lib.beats import analyze_beats

    bpm = analyze_beats(click)["bpm"]
    check("click track is a steady 120 BPM", any(abs(bpm - b) < 4 for b in (60, 120, 240)), f"{bpm} BPM")

    def make(name: str, src: str, fps: int) -> Path:
        out = tmp / name
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                        "-i", f"{src}{':' if '=' in src else '='}s=320x180:r={fps}:d=2",
                        "-pix_fmt", "yuv420p", str(out)], check=True)
        return out

    black = make("black.mp4", "color=c=black", 60)
    picture = make("picture.mp4", "testsrc2", 120)
    info = st.probe_video(picture)
    check("probe: size, fps, duration", (info["width"], info["height"], info["fps"]) == (320, 180, 120.0)
          and 1.9 < info["duration"] < 2.1, str(info))
    check("black clip is black", st.black_share(black, 2.0) > 0.9)
    check("picture isn't black", st.black_share(picture, 2.0) < 0.1)

    r = Report()
    with quiet():
        st.check_clip(r, picture, 120, 180)
    check("a good clip passes", r.fails == 0)
    with quiet():
        st.check_clip(r, picture, 60, 180)
    check("wrong frame rate fails", r.fails == 1)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        st.check_clip(r, black, 60, 180)
    check("black clip fails with the HLAE hint", r.fails == 2 and "HLAE" in out.getvalue())


def test_write_tested(tmp: Path) -> None:
    print("recording tested versions")
    path = tmp / "tested.json"
    st.write_tested(path, {"cs2": "1.41.10.0", "hlae": "2.193.0", "cs_demo_manager": "3.21.0",
                           "demoparser2": "0.43.0", "ffmpeg": "8.1"})
    data = json.loads(path.read_text(encoding="utf-8"))
    check("writes the watched tools, version and date",
          list(data) == ["fragsmith", "tested_on", "cs2", "hlae", "cs_demo_manager", "demoparser2"]
          and data["hlae"] == "2.193.0", str(data))


def test_early_stop(tmp: Path) -> None:
    print("selftest without a demo")
    calls = []
    args = argparse.Namespace(demo=None, player=None, write_tested=False, keep=False, timeout=1)
    (tmp / "compat").mkdir()
    (tmp / "compat" / "tested-versions.json").write_text('{"demoparser2": "0.42.1"}')
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = st.run_selftest(tmp, args, {}, lambda a: calls.append("render") or 0,
                               lambda a: calls.append("finish") or 0)
    text = out.getvalue()
    check("stops with FAIL and a hint", code == 1 and "No demo to test with" in text
          and "[selftest].demo_dir" in text, text[-300:])
    check("nothing was rendered", calls == [])


if __name__ == "__main__":
    test_versions()
    test_picking()
    test_both_render_passes()
    with tempfile.TemporaryDirectory() as t:
        test_media(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_write_tested(Path(t))
    with tempfile.TemporaryDirectory() as t:
        test_early_stop(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All selftest tests OK")
