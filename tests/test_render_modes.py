"""Tests for the render modes (local/ssh/folder), the worker refactor and
`setup`'s config writer. Uses a fake csdm that creates csdm-style output
files, so no CS2 is needed. Windows only (csdm.cmd), like the real thing.

Run: python tests/test_render_modes.py
"""
from __future__ import annotations

import io
import json
import os
import socket
import sys
import tempfile
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import worker  # noqa: E402
from lib.doctor import build_config, run_setup  # noqa: E402
from lib.render_clips import render_local, render_mode  # noqa: E402

FAILS: list[str] = []

FAKE_CSDM = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[0] == "analyze":
    print("analyzed")
    sys.exit(0)
if args[0] == "video":
    if os.environ.get("FAKE_CSDM_FAIL"):
        print("HLAE injection failed")
        sys.exit(3)
    cfg = json.loads(Path(args[2]).read_text(encoding="utf-8"))
    out = Path(cfg["outputFolderPath"])
    skip = os.environ.get("FAKE_CSDM_SKIP", "")
    for s in cfg["sequences"]:
        if str(s["number"]) == skip:
            continue
        name = f"sequence-{s['number']}-tick-{s['startTick']}-to-{s['endTick']}.mp4"
        (out / name).write_text(f"fps={cfg['framerate']}", encoding="utf-8")
    sys.exit(0)
sys.exit(2)
"""


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def make_fake_csdm(tmp: Path) -> Path:
    script = tmp / "fake_csdm.py"
    script.write_text(FAKE_CSDM, encoding="utf-8")
    cmd = tmp / "csdm.cmd"
    cmd.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    return cmd


def make_job(run_id: str) -> dict:
    return {
        "schema_version": "1.3", "run_id": run_id, "demo_filename": "match.dem",
        "pov_steamid": "765me", "pov_name": "Tester", "true_view": True,
        "video": {"width": 1920, "height": 1080, "framerate": 60},
        "clips": [
            {"clip_id": "r03_multikill_00", "number": 1, "start_tick": 100,
             "end_tick": 900, "show_xray": False},
            {"clip_id": "r07_ace_01", "number": 2, "start_tick": 2000,
             "end_tick": 3000, "show_xray": True, "framerate": 120,
             "alt_pov_steamid": "765x", "alt_pov_name": "victim"},
        ],
    }


def test_render_mode() -> None:
    print("render_mode:")
    check("explicit local", render_mode({"mode": "local"}) == "local")
    check("explicit is case-insensitive", render_mode({"mode": "SSH"}) == "ssh")
    check("old config with ssh_host → ssh", render_mode({"ssh_host": "u@h"}) == "ssh")
    check("old config with job_dir → folder", render_mode({"job_dir": "X:/q"}) == "folder")
    check("old config with nothing → folder (USB)", render_mode({}) == "folder")
    try:
        render_mode({"mode": "cloud"})
        check("unknown mode raises", False)
    except ValueError:
        check("unknown mode raises", True)


def test_render_local(tmp: Path, csdm: Path) -> None:
    print("render_local (one-PC mode, fake csdm):")
    run_dir = tmp / "runs" / "local1"
    run_dir.mkdir(parents=True)
    demo = tmp / "match.dem"
    demo.write_bytes(b"demo")
    cfg = {"mode": "local", "csdm_path": str(csdm)}

    result = render_local(make_job("local1"), demo, run_dir, cfg)
    clips = run_dir / "03-raw-clips"
    names = sorted(p.name for p in clips.glob("*.mp4"))
    check("status ok", result["status"] == "ok", result["status"])
    check("clips named by clip_id (+ _alt)",
          names == ["r03_multikill_00.mp4", "r07_ace_01.mp4", "r07_ace_01_alt.mp4"],
          str(names))
    check("high-fps clip came from the 120 fps pass",
          (clips / "r07_ace_01.mp4").read_text() == "fps=120")
    check("demo NOT copied anywhere", not any(run_dir.rglob("*.dem")))
    check("_raw cleaned up", not (clips / "_raw").exists())
    check("csdm configs cleaned up", not list(run_dir.glob("*.csdm-config-*")))
    check("worker.log in the run folder", (run_dir / "worker.log").exists())
    saved = json.loads((run_dir / "render-result.json").read_text(encoding="utf-8"))
    check("render-result.json written", saved["status"] == "ok")

    run2 = tmp / "runs" / "local2"
    run2.mkdir(parents=True)
    os.environ["FAKE_CSDM_SKIP"] = "102"
    try:
        result = render_local(make_job("local2"), demo, run2, cfg)
    finally:
        os.environ.pop("FAKE_CSDM_SKIP")
    check("missing alt clip → partial", result["status"] == "partial", result["status"])
    check("missing clip marked", result["clips"].get("r07_ace_01_alt") == "missing")
    check("_raw kept for troubleshooting", (run2 / "03-raw-clips" / "_raw").is_dir())

    run3 = tmp / "runs" / "local3"
    run3.mkdir(parents=True)
    os.environ["FAKE_CSDM_FAIL"] = "1"
    try:
        render_local(make_job("local3"), demo, run3, cfg)
        check("csdm failure raises", False)
    except RuntimeError as exc:
        check("csdm failure raises with HLAE hint", "HLAE" in str(exc), str(exc)[:80])
    finally:
        os.environ.pop("FAKE_CSDM_FAIL")
    saved = json.loads((run3 / "render-result.json").read_text(encoding="utf-8"))
    check("failure still writes render-result.json",
          saved["status"] == "failed" and "error" in saved)


def test_csdm_app_guard(tmp: Path, csdm: Path) -> None:
    print("CS Demo Manager app open → refuse to render:")
    if worker.csdm_app_running():
        print("  (skipped: something already listens on the csdm port)")
        return
    run_dir = tmp / "runs" / "guard"
    run_dir.mkdir(parents=True)
    demo = tmp / "match.dem"
    demo.write_bytes(b"demo")
    # stand-in for the app: listen on the same port it uses
    with socket.create_server(("localhost", worker.CSDM_GAME_PORT)):
        check("app detected", worker.csdm_app_running())
        try:
            render_local(make_job("guard"), demo, run_dir,
                         {"mode": "local", "csdm_path": str(csdm)})
            check("render refused", False)
        except RuntimeError as exc:
            check("render refused with a clear message",
                  "CS Demo Manager app is open" in str(exc), str(exc)[:60])
    check("app gone → not detected", not worker.csdm_app_running())


def test_queue_still_works(tmp: Path, csdm: Path) -> None:
    print("worker queue (two-PC mode) after the refactor:")
    job_dir = tmp / "queue"
    for sub in ("demos", "inbox", "working", "outbox"):
        (job_dir / sub).mkdir(parents=True)
    (job_dir / "demos" / "match.dem").write_bytes(b"demo")
    job_path = job_dir / "inbox" / "q1.json"
    job_path.write_text(json.dumps(make_job("q1")), encoding="utf-8")

    ok = worker.process_job(job_path, job_dir, csdm)
    result = json.loads((job_dir / "outbox" / "q1.result.json").read_text(encoding="utf-8"))
    check("process_job returns True", ok)
    check("result ok", result["status"] == "ok", result["status"])
    check("clips in outbox/<run_id>/",
          len(list((job_dir / "outbox" / "q1").glob("*.mp4"))) == 3)
    check("job removed from inbox and working",
          not job_path.exists() and not list((job_dir / "working").glob("q1*")))

    job_path = job_dir / "inbox" / "q2.json"
    bad = make_job("q2")
    bad["demo_filename"] = "nope.dem"
    job_path.write_text(json.dumps(bad), encoding="utf-8")
    ok = worker.process_job(job_path, job_dir, csdm)
    result = json.loads((job_dir / "outbox" / "q2.result.json").read_text(encoding="utf-8"))
    check("missing demo → False + error in result",
          not ok and "Demo file missing" in result.get("error", ""))


def test_setup(tmp: Path) -> None:
    print("setup / build_config:")
    example = (ROOT / "config.example.toml").read_text(encoding="utf-8")
    ids = ["76561190000000001", "76561190000000002"]

    local = tomllib.loads(build_config(example, {
        "steamids": ids, "music_dir": r"C:\Music\it's mine",
        "mode": "local", "csdm_path": r"D:\csdm\csdm.cmd",
    }))
    check("local: players", local["players"]["known_players"] == ids)
    check("local: music dir with apostrophe survives",
          local["music"]["music_dir"] == r"C:\Music\it's mine")
    check("local: render mode", local["render"]["mode"] == "local")
    check("local: csdm_path", local["render"]["csdm_path"] == r"D:\csdm\csdm.cmd")
    check("director mode untouched", local["director"]["mode"] == "auto")
    check("other settings keep defaults", local["effects"]["intensity"] == 1.2)

    ssh = tomllib.loads(build_config(example, {
        "steamids": ids[:1], "music_dir": "C:/m", "mode": "ssh",
        "ssh_host": "me@10.0.0.5", "ssh_key": r"C:\Users\me\.ssh\k",
        "remote_job_dir": "C:/jobs",
    }))
    check("ssh: host/key/dir", ssh["render"]["ssh_host"] == "me@10.0.0.5"
          and ssh["render"]["ssh_key"] == r"C:\Users\me\.ssh\k"
          and ssh["render"]["remote_job_dir"] == "C:/jobs")
    check("ssh: mode", ssh["render"]["mode"] == "ssh")

    usb = tomllib.loads(build_config(example, {
        "steamids": ids[:1], "music_dir": "C:/m", "mode": "folder", "job_dir": "",
    }))
    check("usb: folder mode with empty job_dir",
          usb["render"]["mode"] == "folder" and usb["render"]["job_dir"] == "")

    # interactive run with scripted answers, in a throwaway project folder
    proj = tmp / "proj"
    proj.mkdir()
    (proj / "config.example.toml").write_text(example, encoding="utf-8")
    answers = iter(["123", "76561190000000001", str(tmp), "3", "2", "3"])
    with redirect_stdout(io.StringIO()):  # setup ends with a (noisy) doctor run
        run_setup(proj, ask=lambda _prompt: next(answers))
    written = tomllib.loads((proj / "config.toml").read_text(encoding="utf-8"))
    check("run_setup re-asks bad SteamID and bad menu choice, then writes",
          written["players"]["known_players"] == ["76561190000000001"]
          and written["render"]["mode"] == "folder")

    answers = iter(["n"])
    before = (proj / "config.toml").read_text(encoding="utf-8")
    with redirect_stdout(io.StringIO()):  # setup ends with a (noisy) doctor run
        run_setup(proj, ask=lambda _prompt: next(answers))
    check("existing config.toml is not overwritten without 'y'",
          (proj / "config.toml").read_text(encoding="utf-8") == before)


if __name__ == "__main__":
    test_render_mode()
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        if sys.platform == "win32":
            csdm = make_fake_csdm(tmp)
            test_render_local(tmp, csdm)
            test_csdm_app_guard(tmp, csdm)
            test_queue_still_works(tmp, csdm)
        else:
            print("(skipping fake-csdm tests: Windows only)")
        test_setup(tmp)
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All render mode tests OK")
