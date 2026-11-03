"""`python main.py selftest`: does Fragsmith still work with what's installed?

Runs the real pipeline on a small scale, in whatever render mode is configured:
a fresh demo is parsed, three clips are picked (the best, usually rendered at
high fps, a plain one at the base fps, and 2 s around the best kill with
motion blur, so all three render passes run), the render PC renders them,
and they're edited to a generated click track. Every step is checked: the
clips must exist, have the right size and frame rate and not be black (a
broken HLAE can "render" an empty picture), and the blur clip must be blended.

The versions found on this PC and on the render PC are compared with
compat/tested-versions.json. After a PASS, --write-tested records them there.

`cmd_render` and `cmd_finish` come from main.py, so the selftest exercises the
same code as a normal run instead of a copy of it.
"""
from __future__ import annotations

import argparse
import bz2
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from importlib import metadata
from pathlib import Path
from typing import Callable

from lib.doctor import Report
from lib.version import __version__

WATCHED = {"cs2": "CS2", "hlae": "HLAE", "cs_demo_manager": "CS Demo Manager",
           "demoparser2": "demoparser2"}
BLACK_LIMIT = 0.8      # a clip that's black for more than this share failed
POLL_SECONDS = 20


def version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", v))


def compare(found: str | None, tested: str | None) -> str:
    """same / newer / older / unknown"""
    if not found or not tested:
        return "unknown"
    a, b = version_key(found), version_key(tested)
    return "same" if a == b else "newer" if a > b else "older"


def patch_digits(patch_version: str) -> str:
    """steam.inf's PatchVersion as demo headers store it: 1.41.9.4 → 14194."""
    return patch_version.replace(".", "")


def ffmpeg_version() -> str | None:
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
    except FileNotFoundError:
        return None
    m = re.search(r"ffmpeg version \D*(\d+(?:\.\d+)+)", out)
    return m.group(1) if m else "unknown"


def local_versions() -> dict:
    versions = {"fragsmith": __version__, "python": ".".join(map(str, sys.version_info[:3])),
                "ffmpeg": ffmpeg_version()}
    for pkg in ("demoparser2", "librosa"):
        try:
            versions[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            versions[pkg] = None
    return versions


def pick_demo(arg: str | None, cfg: dict) -> Path | None:
    """--demo, or the newest .dem/.dem.bz2 under [selftest].demo_dir."""
    if arg:
        return Path(arg)
    folder = str(cfg.get("selftest", {}).get("demo_dir", "") or "")
    if not folder or not Path(folder).is_dir():
        return None
    demos = [p for p in Path(folder).rglob("*") if p.name.endswith((".dem", ".dem.bz2"))]
    return max(demos, key=lambda p: p.stat().st_mtime, default=None)


def demo_patch(demo: Path) -> str | None:
    """The CS2 version a demo was recorded with (header patch_version, e.g. 14194)."""
    try:
        from demoparser2 import DemoParser

        return str(DemoParser(str(demo)).parse_header().get("patch_version") or "") or None
    except Exception:  # noqa: BLE001 — the parse step reports real problems
        return None


def pick_test_clips(candidates: list) -> list:
    """The best clip, plus the weakest one from another round: the best usually
    gets slow-mo (high fps) and the weak one doesn't, so both render passes
    run. Another round keeps the two apart when the best is a split clutch."""
    best = max(candidates, key=lambda c: c.hype_score)
    others = [c for c in candidates if c.round != best.round]
    if not others:
        return [best]
    return [min(others, key=lambda c: c.hype_score), best]


def first_clip_at_base_fps(plan) -> None:
    """The rules give nearly every multikill a headshot micro slow-mo, which
    means high fps. Turn slow-mo off for the opening clip (the climax comes
    last) so the base-fps render pass runs too (seen in the first live run:
    both clips at 120 fps)."""
    if len(plan.decisions) < 2:
        return
    first = min(plan.decisions, key=lambda d: d.order)
    first.slowmo_segments = []
    first.effects_off = sorted(set(first.effects_off) | {"headshot_punch", "replay"})


BLUR_CLIP_PRE_S = 1.5   # the motion-blur clip: this long before the kill...
BLUR_CLIP_POST_S = 0.5  # ...and this long after (480 fps costs ~11x real time)


def add_motion_blur_clip(plan, chosen: list, cfg: dict, tickrate: int) -> tuple[dict, str | None]:
    """Open the test film with 2 s around the best clip's last kill, motion
    blur forced on, so the motion-blur render pass runs on a short clip
    instead of a long one. The other clips go without blur (the climax keeps
    its replay). Returns (the config to test with, the blur clip's id), with
    [render].motion_blur switched on so the clip's override counts; the
    config is unchanged when there's no kill or no blur frame rate."""
    from lib.models import EditDecision

    base = int(cfg.get("video", {}).get("framerate", 60))
    if int(cfg.get("render", {}).get("motion_blur_framerate", 480)) <= base:
        return cfg, None
    best = max(chosen, key=lambda c: c.hype_score)
    if not best.kills:
        return cfg, None
    kill = best.kills[-1]
    start = max(0, kill.tick - round(BLUR_CLIP_PRE_S * tickrate))
    end = kill.tick + round(BLUR_CLIP_POST_S * tickrate)
    short = best.model_copy(update={
        "clip_id": f"{best.clip_id}_blur", "start_tick": start, "end_tick": end,
        "kill_count": 1, "kind": "highlight_single", "clutch_vs": None,
        "duration_seconds": round((end - start) / tickrate, 1),
        "kills": [kill.model_copy(update={"offset_s": round((kill.tick - start) / tickrate, 3)})],
        "weapons": [kill.weapon], "headshots": int(kill.headshot),
    })
    for d in plan.decisions:
        d.order += 1
        d.effects_off = sorted(set(d.effects_off) | {"motion_blur"})
    plan.decisions.insert(0, EditDecision(
        clip_id=short.clip_id, order=1, slowmo_segments=[], overlay_text=None,
        transition_in="cut", reasoning="Selftest: 2 s with motion blur.",
        effects_on=["motion_blur"],
    ))
    chosen.insert(0, short)
    return {**cfg, "render": {**cfg.get("render", {}), "motion_blur": "climax"}}, short.clip_id


def blended_frames(report_md: Path, clip_id: str) -> int | None:
    """motion_blur_frames for a clip, from the cut log in report.md."""
    text = report_md.read_text(encoding="utf-8") if report_md.exists() else ""
    m = re.search(r"## Cut log.*?```json\n(.*?)```", text, re.S)
    if not m:
        return None
    entry = next((c for c in json.loads(m.group(1)) if c.get("clip_id") == clip_id), None)
    return entry.get("motion_blur_frames") if entry else None


def make_click_track(path: Path, seconds: int = 150) -> None:
    """120 BPM clicks: a steady beat for steps 5–7 that needs no music folder."""
    expr = "0.6*sin(2*PI*880*t)*exp(-40*mod(t\\,0.5))"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", f"aevalsrc={expr}:d={seconds}:s=44100", str(path)],
        check=True, capture_output=True,
    )


def probe_video(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True,
    )
    data = json.loads(out.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    num, _, den = str(stream.get("r_frame_rate", "0/1")).partition("/")
    return {
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
        "width": int(stream.get("width", 0) or 0),
        "height": int(stream.get("height", 0) or 0),
        "fps": round(float(num) / float(den or 1), 1) if num else 0.0,
    }


def black_share(path: Path, duration: float) -> float:
    """Share of the video that ffmpeg's blackdetect calls black (0.0–1.0)."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-an",
         "-vf", "blackdetect=d=0.2:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    black = sum(float(s) for s in re.findall(r"black_duration:([\d.]+)", proc.stderr))
    return min(1.0, black / duration) if duration > 0 else 1.0


def expected_fps(plan, candidates_by_id: dict, cfg: dict) -> dict[str, int]:
    """The frame rate each raw clip should have: the same function step 4 uses."""
    from lib.edit_engine import clip_framerates

    return clip_framerates(plan, candidates_by_id, cfg)


def check_clip(r: Report, path: Path, want_fps: int | None, want_height: int) -> dict:
    info = probe_video(path)
    info["black"] = round(black_share(path, info["duration"]), 2)
    desc = (f"{path.name}: {info['duration']:.1f} s, {info['width']}x{info['height']}, "
            f"{info['fps']:g} fps, {info['black']:.0%} black")
    problems = []
    if info["duration"] < 1:
        problems.append("shorter than a second")
    if want_height and info["height"] != want_height:
        problems.append(f"expected height {want_height}")
    if want_fps and abs(info["fps"] - want_fps) > 1:
        problems.append(f"expected {want_fps} fps")
    if info["black"] > BLACK_LIMIT:
        problems.append("mostly black")
    if problems:
        hint = ("A black clip usually means HLAE doesn't work with this CS2 build: update "
                "HLAE (CS Demo Manager → Settings → Video)." if "mostly black" in problems else "")
        r.fail(f"{desc} — {', '.join(problems)}", hint)
    else:
        r.ok(desc)
    return info


def wait_for_render(mode: str, run_id: str, cfg: dict, raw_dir: Path, timeout_min: float) -> dict:
    """Two-PC modes: poll until the worker has written its result."""
    from lib.render_clips import collect_clips, collect_clips_ssh, job_waiting

    render_cfg = cfg.get("render", {})
    start = time.monotonic()
    last_minute, hinted = -1, False
    while True:
        if mode == "ssh":
            _, result = collect_clips_ssh(run_id, render_cfg, raw_dir)
        else:
            _, result = collect_clips(run_id, Path(render_cfg.get("job_dir", "")), raw_dir)
        if result:
            return result
        waited = (time.monotonic() - start) / 60
        if waited > timeout_min:
            return {"status": "timeout", "error": f"no result from the render PC after {timeout_min:g} min"}
        if int(waited) != last_minute:
            last_minute = int(waited)
            # Still in the inbox after a minute means no worker picked it up;
            # once it's taken, the worker is busy rendering, which takes minutes.
            if waited >= 1 and not hinted and job_waiting(run_id, render_cfg):
                print("    The job is still in the render PC's inbox: is the worker running there? "
                      "(python worker.py --job-dir <job folder>)")
                hinted = True
            else:
                print(f"    waiting for the render PC ... {last_minute} min", flush=True)
        time.sleep(POLL_SECONDS)


def write_tested(path: Path, versions: dict) -> None:
    data = {"fragsmith": __version__, "tested_on": date.today().isoformat()}
    data.update({k: versions[k] for k in WATCHED})
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _section(title: str) -> None:
    print(f"\n{title}")


def run_selftest(project_dir: Path, args: argparse.Namespace, cfg: dict,
                 render_cmd: Callable[[argparse.Namespace], int],
                 finish_cmd: Callable[[argparse.Namespace], int]) -> int:
    from lib.director import DirectorError
    from lib.hype_score import build_candidates
    from lib.parse_demo import DemoParseError, parse_demo
    from lib.render_clips import render_mode
    from lib.rules_director import run_rules_director

    print(f"Fragsmith {__version__} selftest")
    r = Report()
    tested_path = project_dir / "compat" / "tested-versions.json"
    tested = json.loads(tested_path.read_text(encoding="utf-8")) if tested_path.exists() else {}
    report: dict = {"fragsmith": __version__, "started": datetime.now().isoformat(timespec="seconds"),
                    "versions": {}, "tested": tested}
    run_dir: Path | None = None

    def done() -> int:
        verdict = "PASS" if r.fails == 0 else "FAIL"
        report.update(result=verdict, fails=r.fails, warns=r.warns,
                      finished=datetime.now().isoformat(timespec="seconds"))
        if run_dir:
            (run_dir / "selftest-report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print()
        print("=" * 64)
        if verdict == "FAIL":
            print(f"SELFTEST FAIL: {r.fails} problem(s). Fix the FAIL lines above.")
            if run_dir:
                print(f"Everything is kept for troubleshooting in {run_dir}")
            print("When an update broke something: docs/compatibility.md#when-an-update-breaks-things")
            print("=" * 64)
            return 1
        print(f"SELFTEST PASS: Fragsmith {__version__} works with what's installed.")
        newer = [WATCHED[k] for k in WATCHED if compare(report["versions"].get(k), tested.get(k)) == "newer"]
        unknown = [WATCHED[k] for k in WATCHED if not report["versions"].get(k)]
        if args.write_tested and unknown:
            print(f"Not recorded: the version of {', '.join(unknown)} is unknown.")
        elif args.write_tested:
            write_tested(tested_path, report["versions"])
            print(f"Recorded the versions in {tested_path.relative_to(project_dir)}.")
            print("Commit it in a PR to develop, e.g. 'chore: record tested versions'.")
        elif newer:
            print(f"Newer than tested: {', '.join(newer)}. Run again with --write-tested "
                  f"to record them in compat/tested-versions.json.")
        if run_dir and not args.keep:
            for sub in ("03-raw-clips", "04-segments"):
                shutil.rmtree(run_dir / sub, ignore_errors=True)
        if run_dir:
            kept = "kept (--keep)" if args.keep else "deleted (--keep keeps them)"
            print(f"The test film: {run_dir / 'final.mp4'} (raw clips {kept})")
        print("=" * 64)
        return 0

    # --- Versions on this PC -------------------------------------------------
    _section("Versions on this PC")
    local = local_versions()
    report["versions"].update(demoparser2=local["demoparser2"])
    report["local"] = local
    for name, key in (("Fragsmith", "fragsmith"), ("Python", "python"), ("ffmpeg", "ffmpeg"),
                      ("demoparser2", "demoparser2"), ("librosa", "librosa")):
        v = local[key]
        if v is None:
            r.fail(f"{name} not found", "pip install -r requirements.txt / install ffmpeg")
            continue
        state = compare(v, tested.get(key)) if key in WATCHED else "same"
        note = {"newer": f" (newer than tested {tested.get(key)})",
                "older": f" (older than tested {tested.get(key)})"}.get(state, "")
        r.ok(f"{name} {v}{note}")
    if r.fails:
        return done()

    # --- Demo ---------------------------------------------------------------
    _section("Demo")
    demo = pick_demo(args.demo, cfg)
    if demo is None or not demo.exists():
        r.fail("No demo to test with",
               "Pass --demo <file.dem>, or set [selftest].demo_dir in config.toml to a "
               "folder with fresh demos (the newest is used).")
        return done()
    if demo.name.endswith(".bz2"):
        target = demo.with_suffix("")
        if not target.exists():
            with bz2.open(demo, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        demo = target
    patch = demo_patch(demo)
    report.update(demo=demo.name, demo_patch=patch)
    r.ok(f"{demo.name}" + (f" (recorded with CS2 patch {patch})" if patch else ""))

    # --- Steps 1-3 ----------------------------------------------------------
    _section("Steps 1–3: parse, pick test clips, edit plan")
    run_id = f"selftest-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = project_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        events = parse_demo(demo)
    except DemoParseError as exc:
        r.fail(f"demoparser2 couldn't parse the demo: {exc}",
               "A new demoparser2 can break parsing: see docs/compatibility.md.")
        return done()
    r.ok(f"parsed: {events['map'] or 'unknown map'}, {len(events['rounds'])} rounds, "
         f"{len(events['kills'])} kills")
    candidates, _, pov = build_candidates(events, cfg, args.player)
    if not candidates:
        r.fail("No highlight in this demo for the POV player",
               "Use another demo, or --player <steamid64> for someone with multikills.")
        return done()
    chosen = pick_test_clips(candidates)
    pov_name = next((p["name"] for p in events["players"] if p["steamid"] == pov), "")
    click = run_dir / "selftest-click.wav"
    make_click_track(click)
    try:
        plan = run_rules_director(chosen, events, [click.name], pov_name)
    except DirectorError as exc:
        r.fail(f"The rules Director failed: {exc}")
        return done()
    first_clip_at_base_fps(plan)
    cfg, blur_id = add_motion_blur_clip(plan, chosen, cfg, int(events.get("tickrate", 64)))
    (run_dir / "00-events.json").write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    (run_dir / "01-candidates.json").write_text(json.dumps(
        {"pov_steamid": pov, "candidates": [c.model_dump() for c in chosen], "flags": []},
        ensure_ascii=False), encoding="utf-8")
    (run_dir / "02-edit-plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    by_id = {c.clip_id: c for c in chosen}
    fps = expected_fps(plan, by_id, cfg)
    r.ok("test clips: " + ", ".join(
        f"{c.clip_id} ({c.duration_seconds:.0f} s, {fps.get(c.clip_id)} fps)" for c in chosen))

    # --- Step 4 -------------------------------------------------------------
    mode = render_mode(cfg.get("render", {}))
    _section(f"Step 4: rendering ({mode} mode) — CS2 takes over the render PC's screen")
    raw_dir = run_dir / "03-raw-clips"
    code = render_cmd(argparse.Namespace(run_dir=str(run_dir), cfg=cfg))
    if mode == "local":
        result_path = run_dir / "render-result.json"
        result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.exists() else {}
    elif code == 0:
        result = wait_for_render(mode, run_id, cfg, raw_dir, args.timeout)
    else:
        result = {"status": "failed", "error": "the job couldn't be sent (see above)"}
    render_versions = result.get("versions") or {}
    report["render"] = {k: v for k, v in result.items() if k in ("status", "error", "clips", "warnings")}
    report["render_versions"] = render_versions
    report["versions"].update({k: render_versions.get(k) for k in ("cs2", "hlae", "cs_demo_manager")})

    print()
    if result.get("status") in ("ok", "partial"):
        r.ok(f"render {result['status']}: {sum(v == 'ok' for v in result.get('clips', {}).values())}"
             f"/{len(result.get('clips', {}))} clips")
    else:
        r.fail(f"render failed: {result.get('error', 'no result')}")
    height = int(cfg.get("video", {}).get("height", 1080))
    for clip in sorted(raw_dir.glob("*.mp4")) if raw_dir.is_dir() else []:
        check_clip(r, clip, fps.get(clip.stem), height)
    if result.get("status") == "partial":
        r.fail("some clips are missing: " + ", ".join(
            k for k, v in result.get("clips", {}).items() if v != "ok"))

    # --- Versions on the render PC -------------------------------------------
    _section("Versions on the render PC" if mode != "local" else "Versions of CS2 and its tools")
    if not render_versions:
        r.warn("The render result has no versions",
               "The worker on the render PC is older than this Fragsmith: copy worker.py "
               "there and restart it (docs/two-pc-setup.md).")
    for key in ("cs2", "hlae", "cs_demo_manager"):
        if not render_versions:
            break
        v = render_versions.get(key)
        state = compare(v, tested.get(key))
        if v is None:
            r.warn(f"{WATCHED[key]}: version not found")
        elif state == "newer":
            r.ok(f"{WATCHED[key]} {v} (newer than tested {tested.get(key)})")
        elif state == "older":
            r.warn(f"{WATCHED[key]} {v} (older than tested {tested.get(key)})",
                   "Update it on the render PC.")
        else:
            r.ok(f"{WATCHED[key]} {v}")
    if patch and render_versions.get("cs2") and patch != patch_digits(render_versions["cs2"]):
        r.warn(f"The demo is from CS2 patch {patch}, the render PC runs {render_versions['cs2']}",
               "Old demos can stop playing after a CS2 update; test with a fresh demo.")
    if r.fails:
        return done()

    # --- Steps 5-7 ----------------------------------------------------------
    _section("Steps 5–7: beats, editing, assembly (to a 120 BPM click track)")
    code = finish_cmd(argparse.Namespace(run_dir=str(run_dir), music=str(click), clips=None,
                                         cfg=cfg))
    final = run_dir / "final.mp4"
    print()
    if code != 0 or not final.exists():
        r.fail("steps 5–7 failed (see the output above and ffmpeg.log)")
        return done()
    info = check_clip(r, final, None, height)
    report["final"] = info
    if blur_id:
        from lib.edit_engine import blur_frames

        render_cfg = cfg.get("render", {})
        want = blur_frames(fps.get(blur_id, 60), float(render_cfg.get("motion_blur_shutter", 180)), 1.0)
        got = blended_frames(run_dir / "report.md", blur_id)
        if got == want:
            r.ok(f"motion blur: {got} frames blended per frame in {blur_id}")
        else:
            r.fail(f"motion blur: {got} frames blended per frame in {blur_id}, expected {want}")
    return done()
