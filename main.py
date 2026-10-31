"""Fragsmith — CS2 .dem → fragmovie.

First time:
    python main.py setup               writes config.toml (one PC or two)
    python main.py doctor              checks that everything is in place

Workflow:
    python main.py run <demo.dem>      steps 0-3, stops at the edit plan
    python main.py render runs/<id>    step 4, renders the clips (approval)
    python main.py finish runs/<id>    steps 5-7, creates final.mp4
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from lib.hype_score import build_candidates  # noqa: E402
from lib.parse_demo import DemoParseError, parse_demo  # noqa: E402


def load_dotenv() -> None:
    """Minimal .env loader (KEY=VALUE per line); never overrides existing vars."""
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    import os

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def read_learnings() -> None:
    """Step 0: read learnings_runtime.md BEFORE anything else (fixed rule)."""
    path = PROJECT_DIR / "learnings_runtime.md"
    if not path.exists():
        print("WARNING: learnings_runtime.md is missing!")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [l for l in lines if l.startswith("- 20")]
    print(f"Step 0: read learnings_runtime.md ({len(entries)} entries)")
    for entry in entries:
        print(f"    {entry}")


def load_config() -> dict:
    path = PROJECT_DIR / "config.toml"
    if not path.exists():
        print(f"ERROR: cannot find {path}")
        print("Copy config.example.toml to config.toml and fill in your values.")
        sys.exit(1)
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _resolve_demo(path_arg: str) -> Path:
    """Accept .dem or .dem.bz2 (Valve MM stores bz2) — extract if needed."""
    demo = Path(path_arg)
    if demo.suffix == ".bz2":
        target = demo.with_suffix("")
        if not target.exists():
            print(f"Extracting {demo.name} ...")
            import bz2
            import shutil

            with bz2.open(demo, "rb") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
        return target
    return demo


def cmd_run(args: argparse.Namespace) -> int:
    read_learnings()
    cfg = load_config()

    demo = _resolve_demo(args.demo)
    run_id = f"{demo.stem}-{datetime.now():%Y%m%d-%H%M%S}"
    run_dir = PROJECT_DIR / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Run: runs/{run_id}/")

    # --- Step 1: parse demo ----------------------------------------------
    print(f"Step 1: parsing {demo.name} ...")
    try:
        events = parse_demo(demo)
    except DemoParseError as exc:
        print(f"STOPPED in step 1: {exc}")
        (run_dir / "error.json").write_text(
            json.dumps({"step": 1, "error": str(exc)}, indent=2), encoding="utf-8"
        )
        return 1

    (run_dir / "00-events.json").write_text(
        json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"    map: {events['map'] or 'unknown'} | "
        f"{len(events['rounds'])} rounds | {len(events['kills'])} kills | "
        f"{len(events['players'])} players"
    )

    # --- Step 2: hype scoring ---------------------------------------------
    print("Step 2: hype scoring ...")
    candidates, flags, pov = build_candidates(events, cfg, args.player)
    (run_dir / "01-candidates.json").write_text(
        json.dumps(
            {
                "pov_steamid": pov,
                "candidates": [c.model_dump() for c in candidates],
                "flags": flags,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    pov_name = next(
        (p["name"] for p in events["players"] if p["steamid"] == pov), pov
    )
    print(f"    POV: {pov_name} ({pov})")
    print(f"    {len(candidates)} candidate clips:")
    print(f"    {'clip':<22} {'type':<17} {'kills':>5} {'score':>6} {'sec':>6}")
    for c in candidates:
        print(
            f"    {c.clip_id:<22} {c.kind:<17} {c.kill_count:>5} "
            f"{c.hype_score:>6.2f} {c.duration_seconds:>6.1f}"
        )

    if flags:
        print("Flags:")
        for f in flags:
            print(f"    ! {f}")

    if not candidates:
        print("No candidate clips — stopping here (nothing to make a film from).")
        return 0

    # --- Step 3: Director (Claude or rules) -------------------------------
    import os

    from lib.director import DirectorError, list_music_files

    dcfg = cfg.get("director", {})
    mode = str(getattr(args, "director", None) or dcfg.get("mode", "auto")).lower()
    if mode not in ("auto", "rules", "claude"):
        print(f"ERROR: unknown Director mode '{mode}' (use auto, rules or claude).")
        return 1
    load_dotenv()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if mode == "claude" and not has_key:
        print(
            "STOPPED before step 3: Director mode is 'claude' but "
            "ANTHROPIC_API_KEY is missing. Put the key in a .env file in the "
            "project folder, or use --director rules."
        )
        return 1

    music_dir = args.music or cfg.get("music", {}).get("music_dir", "")
    music_files = list_music_files(music_dir)
    if not music_files:
        print(
            f"STOPPED before step 3: no music files found in '{music_dir}'. "
            f"Put mp3/wav/flac files there, or point to them with --music."
        )
        return 1

    plan = None
    director_label = "rules"
    if mode == "claude" or (mode == "auto" and has_key):
        model = dcfg.get("model", "claude-opus-4-8")
        print(f"Step 3: AI Director ({model}) ...")
        try:
            from lib.director import run_director

            plan = run_director(
                candidates=candidates,
                events=events,
                music_files=music_files,
                pov_name=pov_name,
                run_dir=run_dir,
                model=model,
                max_retries=int(dcfg.get("max_retries", 3)),
            )
            director_label = f"Claude ({model})"
        except DirectorError as exc:
            if mode == "claude":
                print(f"STOPPED in step 3: {exc}")
                (run_dir / "error.json").write_text(
                    json.dumps({"step": 3, "error": str(exc)}, indent=2),
                    encoding="utf-8",
                )
                return 1
            print(f"    Claude failed: {exc}")
            print("    Falling back to the rule-based Director.")
    if plan is None:
        note = " (no ANTHROPIC_API_KEY found)" if mode == "auto" and not has_key else ""
        print(f"Step 3: rule-based Director{note} ...")
        from lib.rules_director import run_rules_director

        try:
            plan = run_rules_director(candidates, events, music_files, pov_name)
        except DirectorError as exc:
            print(f"STOPPED in step 3: {exc}")
            (run_dir / "error.json").write_text(
                json.dumps({"step": 3, "error": str(exc)}, indent=2),
                encoding="utf-8",
            )
            return 1

    (run_dir / "02-edit-plan.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )

    # --- PHASE STOP: show edit plan + estimates, wait for approval ---------
    from lib.edit_engine import pick_replay_clip, wants_high_fps

    by_id = {c.clip_id: c for c in candidates}
    chosen = sorted(plan.decisions, key=lambda d: d.order)
    total_sec = sum(by_id[d.clip_id].duration_seconds for d in chosen)

    # Slow-mo clips are rendered at high fps — same selection as step 4 makes
    fx = cfg.get("effects", {})
    base_fps = int(cfg.get("video", {}).get("framerate", 60))
    high_fps = int(cfg.get("render", {}).get("highfps_framerate", 0))
    if high_fps <= base_fps:
        high_fps = 0
    replay = pick_replay_clip(plan, by_id, cfg)
    highfps_ids = {
        d.clip_id for d in chosen
        if high_fps and (
            wants_high_fps(d, by_id[d.clip_id], fx)
            or (replay and replay[0] == d.clip_id)
        )
    }
    high_sec = sum(by_id[cid].duration_seconds for cid in highfps_ids)

    # Rough estimates: rendering ~2x real time + startup; raw clips ~40 MB/min
    # at 1080p60. High-fps clips scale ~linearly with fps (more frames), and
    # two passes means one extra CS2 launch (~2 min). Two passes only when
    # the film mixes base-fps and high-fps clips.
    two_passes = bool(highfps_ids) and len(highfps_ids) < len(chosen)
    fps_scale = high_fps / base_fps if high_fps else 1.0
    est_render_min = (
        total_sec * 2 + high_sec * 2 * (fps_scale - 1)
        + 120 + (120 if two_passes else 0)
    ) / 60
    est_disk_mb = (total_sec + high_sec * (fps_scale - 1)) / 60 * 40

    print()
    print("=" * 64)
    print("EDIT PLAN (phase stop — rendering requires your approval)")
    print(f"  Director: {director_label}")
    print(f"  Music: {plan.music_track}")
    print(f"  Intro: {plan.intro_text}   Outro: {plan.outro_text}")
    print(f"  {'#':>2} {'clip':<22} {'score':>6} {'sec':>6}  overlay/slow-mo")
    for d in chosen:
        c = by_id[d.clip_id]
        extras = []
        if d.overlay_text:
            extras.append(f'"{d.overlay_text}"')
        if d.slowmo_segments:
            extras.append(f"{len(d.slowmo_segments)} slow-mo")
        if d.clip_id in highfps_ids:
            extras.append(f"{high_fps} fps")
        print(
            f"  {d.order:>2} {d.clip_id:<22} {c.hype_score:>6.2f} "
            f"{c.duration_seconds:>6.1f}  {', '.join(extras)}"
        )
    if plan.dropped_clip_ids:
        print(f"  Dropped: {', '.join(plan.dropped_clip_ids)}")
    if highfps_ids:
        print(
            f"  {len(highfps_ids)} clip(s) rendered at {high_fps} fps "
            f"(real smooth slow-mo)"
            + (" — rendering runs in 2 passes." if two_passes else ".")
        )
    print(
        f"  Total clip time: {total_sec:.0f} s | rough render estimate: "
        f"~{est_render_min:.0f} min | raw clip disk: ~{est_disk_mb:.0f} MB"
    )
    print("=" * 64)
    print("Reasoning per clip is in 02-edit-plan.json (reasoning).")
    print(f"Approve and render: python main.py render runs/{run_id}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Step 4: send the render job to the worker.

    Running this command IS the approval from the phase stop — it queues the
    job that starts CS2 rendering.
    """
    read_learnings()
    cfg = load_config()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    run_id = run_dir.name
    plan_path = run_dir / "02-edit-plan.json"
    if not plan_path.exists():
        print(f"ERROR: cannot find {plan_path} — run 'run' first.")
        return 1

    from lib.models import EditPlan, Highlight
    from lib.render_clips import build_render_job, submit_job

    plan = EditPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    cand_data = json.loads(
        (run_dir / "01-candidates.json").read_text(encoding="utf-8")
    )
    candidates_by_id = {
        c["clip_id"]: Highlight.model_validate(c) for c in cand_data["candidates"]
    }
    pov_steamid = cand_data["pov_steamid"]
    pov_name = next(
        (c.player_name for c in candidates_by_id.values() if c.player_name), ""
    )

    job = build_render_job(run_id, plan, candidates_by_id, pov_steamid, pov_name, cfg)
    high = [c for c in job["clips"] if c.get("framerate")]
    if high:
        two_passes = len(high) < len(job["clips"])
        print(
            f"{len(high)} clip(s) rendered at {high[0]['framerate']} fps "
            f"({', '.join(c['clip_id'] for c in high)})"
            + (" — 2 render passes (one extra CS2 launch)." if two_passes else ".")
        )
    n_alt = sum(1 for c in job["clips"] if c.get("alt_pov_steamid"))
    if n_alt:
        alt = next(c for c in job["clips"] if c.get("alt_pov_steamid"))
        print(
            f"Instant replay: {alt['clip_id']} gets an EXTRA victim-POV sequence "
            f"({alt.get('alt_pov_name', '?')}) — a bit more render time and disk."
        )

    demo_path = Path(plan.demo_path)
    if not demo_path.exists():
        print(f"ERROR: cannot find the demo file {demo_path} (needed for copying).")
        return 1

    from lib.render_clips import csdm_path, render_local, render_mode

    render_cfg = cfg.get("render", {})
    try:
        mode = render_mode(render_cfg)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    if mode == "local":
        csdm = csdm_path(render_cfg)
        if not csdm.exists():
            print(
                f"ERROR: cannot find CS Demo Manager at {csdm}. Install it, or "
                f"set [render].csdm_path. 'python main.py doctor' checks the setup."
            )
            return 1
        n_seq = len(job["clips"]) + n_alt
        print(f"Rendering {n_seq} sequences on this PC (one-PC mode).")
        print("CS2 will start and take over the screen — don't use the PC "
              "until it closes again.")
        try:
            result = render_local(job, demo_path, run_dir, render_cfg)
        except RuntimeError as exc:
            print(f"STOPPED in step 4: {exc}")
            print(f"Log: {run_dir / 'worker.log'}")
            return 1
        done = sum(1 for v in result["clips"].values() if v == "ok")
        print(f"Render {result['status']}: {done}/{len(result['clips'])} clips "
              f"in {run_dir / '03-raw-clips'}")
        for w in result.get("warnings", []):
            print(f"    ! {w}")
        if result["status"] == "failed":
            print(f"STOPPED: {result.get('error', 'no clips were produced.')}")
            print(f"Log: {run_dir / 'worker.log'}")
            return 1
        print(f"Next: python main.py finish {args.run_dir}")
        return 0

    if mode == "ssh":
        from lib.render_clips import SshError, submit_job_ssh

        if not render_cfg.get("ssh_host"):
            print("ERROR: [render].mode is 'ssh' but ssh_host is empty in config.toml.")
            return 1

        print(f"Sending job via SSH to {render_cfg['ssh_host']} ...")
        try:
            job_ref = submit_job_ssh(job, demo_path, render_cfg)
        except SshError as exc:
            print(f"STOPPED: {exc}")
            return 1
        print(f"Job queued: {job_ref}")
        print(f"{len(job['clips']) + n_alt} sequences will render when the worker picks up the job.")
        print("Start the worker on the render PC if it isn't running "
              "(python worker.py --job-dir <job folder>).")
        print(f"When done: python main.py finish {args.run_dir}")
        return 0

    job_dir_cfg = render_cfg.get("job_dir", "")
    if not job_dir_cfg:
        # No job folder configured — write the job locally for manual (USB) transfer
        job_path = run_dir / f"{run_id}.render-job.json"
        job_path.write_text(
            json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Job file written: {job_path}")
        print("render.job_dir is not set in config.toml — manual transfer:")
        print(f"  1. Copy the job file to <job folder>/inbox/ on the render PC")
        print(f"  2. Copy {demo_path.name} to <job folder>/demos/")
        print(f"  3. Run the worker there: python worker.py --job-dir <job folder>")
        print(f"  4. Copy <job folder>/outbox/{run_id}/ back here as 03-raw-clips/")
        return 0

    job_dir = Path(job_dir_cfg)
    print(f"Sending job to queue: {job_dir}")
    job_path = submit_job(job, demo_path, job_dir)
    print(f"Job queued: {job_path}")
    print(f"{len(job['clips']) + n_alt} sequences will render when the worker picks up the job.")
    print(f"When done: python main.py finish {args.run_dir}")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Steps 5-7: beats + editing + assembly. Requires raw clips (step 4) in
    <run-dir>/03-raw-clips/ (or --clips)."""
    read_learnings()
    cfg = load_config()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    plan_path = run_dir / "02-edit-plan.json"
    cand_path = run_dir / "01-candidates.json"
    if not plan_path.exists():
        print(f"ERROR: cannot find {plan_path} — run 'run' first.")
        return 1

    from lib.assemble import assemble, build_intro_hook, write_report
    from lib.beats import analyze_beats
    from lib.edit_engine import build_segments, plan_drop_sync
    from lib.ffmpeg_util import FfmpegError, probe_duration
    from lib.models import EditPlan, Highlight

    plan = EditPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    if args.music:
        plan.music_track = args.music  # override the Director's music choice
    cand_data = json.loads(cand_path.read_text(encoding="utf-8"))
    candidates_by_id = {
        c["clip_id"]: Highlight.model_validate(c) for c in cand_data["candidates"]
    }

    # Demo context for the lower third (map) and the stats outro
    events: dict = {}
    events_path = run_dir / "00-events.json"
    if events_path.exists():
        events = json.loads(events_path.read_text(encoding="utf-8"))

    clips_dir = Path(args.clips) if args.clips else run_dir / "03-raw-clips"
    if not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4")):
        from lib.render_clips import render_mode

        render_cfg = cfg.get("render", {})
        try:
            mode = render_mode(render_cfg)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        # Fetch automatically via SSH if configured (local mode renders
        # straight into 03-raw-clips, so there's nothing to fetch)
        if mode == "ssh" and render_cfg.get("ssh_host"):
            from lib.render_clips import collect_clips_ssh

            copied, result = collect_clips_ssh(
                run_dir.name, render_cfg, run_dir / "03-raw-clips"
            )
            if copied:
                print(f"Fetched {copied} raw clips from the render PC via SSH.")
                clips_dir = run_dir / "03-raw-clips"
                for w in result.get("warnings", []):
                    print(f"    ! worker warning: {w}")
            elif result:
                print(
                    f"The render job has status '{result.get('status')}'"
                    + (f": {result.get('error')}" if result.get("error") else "")
                )
            else:
                print("No result.json from the worker yet — the job isn't finished.")
        # Otherwise: fetch from the shared/local job folder if configured
        job_dir_cfg = render_cfg.get("job_dir", "") if mode == "folder" else ""
        if job_dir_cfg and (not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4"))):
            from lib.render_clips import collect_clips

            copied, result = collect_clips(
                run_dir.name, Path(job_dir_cfg), run_dir / "03-raw-clips"
            )
            if copied:
                print(f"Fetched {copied} raw clips from the render queue's outbox.")
                clips_dir = run_dir / "03-raw-clips"
                for w in result.get("warnings", []):
                    print(f"    ! worker warning: {w}")
            elif result:
                print(
                    f"The render job has status '{result.get('status')}'"
                    + (f": {result.get('error')}" if result.get("error") else "")
                )
    if not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4")):
        print(
            f"ERROR: no raw clips in {clips_dir}. Run 'render' and wait for "
            f"the worker, or point to them with --clips."
        )
        return 1

    music_dir = Path(cfg.get("music", {}).get("music_dir", ""))
    music_path = Path(plan.music_track)
    if not music_path.is_absolute():
        music_path = music_dir / plan.music_track
    if not music_path.exists():
        print(f"ERROR: cannot find music track: {music_path}")
        return 1

    log_path = run_dir / "ffmpeg.log"

    print(f"Step 5: beat analysis of {music_path.name} ...")
    beats = analyze_beats(music_path)
    (run_dir / "05-beats.json").write_text(
        json.dumps(beats, indent=2), encoding="utf-8"
    )
    print(f"    {beats['bpm']} BPM, {len(beats['beat_times'])} beats")
    if beats.get("drops"):
        drop_str = ", ".join(f"{d['time_s']:.1f}s" for d in beats["drops"])
        print(f"    drops found: {drop_str} (strongest first)")

    # The cold open glimpses sit BEFORE the intro on the timeline — they must
    # be built now so step 6 knows where the segments actually start
    # (otherwise beat sync drifts by the length of the cold open).
    work_dir = run_dir / "04-segments"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        hooks = build_intro_hook(
            plan, candidates_by_id, clips_dir, work_dir, cfg, log_path
        )
    except FfmpegError as exc:
        print(f"STOPPED in step 6 (cold open): {exc}")
        return 1
    hook_s = sum(probe_duration(h) for h in hooks)

    # Drop sync: place the climax clip on the music's drop if possible
    editing = cfg.get("editing", {})
    intro_s = float(editing.get("intro_seconds", 2.5))
    music_offset = 0.0
    beats_used = beats
    drop_info = plan_drop_sync(
        plan, candidates_by_id, clips_dir, beats, cfg, hook_s
    )
    if drop_info:
        beats_used = drop_info["beats"]
        intro_s = drop_info["intro_seconds"]
        music_offset = drop_info["music_offset"]
        method = (
            f"intro card adjusted to {intro_s:.1f} s"
            if drop_info["method"] == "intro"
            else f"music starts {music_offset:.1f} s into the track"
        )
        print(
            f"    Drop sync: climax ({drop_info['climax_clip_id']}) placed on "
            f"the drop at {drop_info['drop_track_s']:.1f} s — {method}"
        )
    elif editing.get("drop_sync", False):
        why = (
            "no drop candidate could be reached"
            if beats.get("drops")
            else "no clear drop in the track"
        )
        print(f"    Drop sync: {why} — climax at the end instead.")

    print("Step 6: editing engine (slow-mo, overlays, beat alignment) ...")
    try:
        segments, cut_log, flags = build_segments(
            plan, candidates_by_id, clips_dir,
            work_dir, beats_used, cfg, log_path,
            context={"map": events.get("map", "")},
            timeline_start=hook_s + intro_s,
        )
    except FfmpegError as exc:
        print(f"STOPPED in step 6: {exc}")
        return 1
    if not segments:
        print("STOPPED: no segments could be built (are all raw clips missing?).")
        return 1
    print(f"    {len(segments)} segments built")

    # Drop sync: measure the ACTUAL offset from the drop (encoding is frame-granular)
    if drop_info:
        entry = next(
            (c for c in cut_log if c["clip_id"] == drop_info["climax_clip_id"]),
            None,
        )
        if entry:
            actual_start = entry["timeline_end_s"] - entry["segment_duration_s"]
            err_ms = round(
                (actual_start - drop_info["drop_timeline_s"]) * 1000
            )
            drop_info["actual_error_ms"] = err_ms
            print(f"    Drop sync: climax lands {err_ms:+d} ms from the drop")
        beats_used = dict(beats_used)
        beats_used["drop_sync"] = {
            k: v for k, v in drop_info.items() if k != "beats"
        }

    print("Step 7: assembly (intro, music mix, export) ...")
    try:
        final = assemble(
            plan, segments, music_path, run_dir, cfg, log_path,
            candidates_by_id=candidates_by_id, clips_dir=clips_dir,
            events=events, cut_log=cut_log,
            hook_files=hooks, intro_seconds=intro_s,
            music_offset=music_offset,
        )
    except FfmpegError as exc:
        print(f"STOPPED in step 7: {exc}")
        return 1

    film_s = probe_duration(final)
    music_left = beats["duration_seconds"] - music_offset
    if music_left < film_s - 0.5:
        flags.append(
            f"The music ({plan.music_track}) runs out {film_s - music_left:.0f} s "
            f"before the film ends, so it fades out early. Pick a longer track "
            f"with: python main.py finish {args.run_dir} --music <file>"
        )

    report = write_report(
        plan, candidates_by_id, cut_log, flags, beats_used, final, run_dir,
        events=events,
    )

    if flags:
        print("Flags:")
        for f in flags:
            print(f"    ! {f}")
    print()
    print(f"DONE: {final}")
    print(f"Report: {report}")
    print(f"Raw clips are kept until you've watched the film — "
          f"delete them with: python main.py cleanup {args.run_dir}")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Process every demo in a folder sequentially (steps 0-3).

    processed.json remembers what has been run, so a batch can be resumed
    without duplicate work. Rendering NEVER happens in parallel — the worker
    takes jobs one at a time.
    """
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"ERROR: {folder} is not a folder.")
        return 1

    processed_path = PROJECT_DIR / "processed.json"
    processed: dict = {}
    if processed_path.exists():
        processed = json.loads(processed_path.read_text(encoding="utf-8"))

    # .dem is preferred; .bz2 is included where the .dem isn't extracted yet
    demos: dict[str, Path] = {}
    for f in sorted(folder.iterdir()):
        if f.suffix == ".dem":
            demos[f.stem] = f
        elif f.name.endswith(".dem.bz2"):
            demos.setdefault(f.name[: -len(".dem.bz2")], f)

    todo = {k: v for k, v in demos.items() if args.force or k not in processed}
    print(f"Batch: {len(demos)} demos found, {len(todo)} unprocessed.")

    failed = 0
    for i, (stem, path) in enumerate(todo.items(), start=1):
        print(f"\n--- [{i}/{len(todo)}] {path.name} " + "-" * 20)
        code = cmd_run(
            argparse.Namespace(
                demo=str(path), player=args.player, music=None,
                director=args.director,
            )
        )
        processed[stem] = {
            "demo": path.name,
            "status": "ok" if code == 0 else "failed",
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        processed_path.write_text(
            json.dumps(processed, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        failed += code != 0

    print(f"\nBatch done: {len(todo) - failed} ok, {failed} failed.")
    print("Next: review the edit plans and render the ones you want "
          "(python main.py render runs/<id>).")
    return 1 if failed else 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Delete temp files (raw clips + segments) for a run. Keeps
    final.mp4, report.md and all json artifacts."""
    import shutil

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    if not (run_dir / "final.mp4").exists() and not args.force:
        print(
            "STOPPED: final.mp4 doesn't exist in this run — deleting the raw "
            "clips now means rendering again. Use --force if you're sure."
        )
        return 1
    freed = 0
    for sub in ("03-raw-clips", "04-segments"):
        target = run_dir / sub
        if target.is_dir():
            freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target)
            print(f"Deleted {sub}/")
    print(f"Freed ~{freed / 1e6:.0f} MB.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from lib.doctor import run_doctor

    load_dotenv()
    return run_doctor(PROJECT_DIR)


def cmd_setup(args: argparse.Namespace) -> int:
    from lib.doctor import run_setup

    load_dotenv()
    try:
        return run_setup(PROJECT_DIR)
    except (KeyboardInterrupt, EOFError):
        print("\nSetup cancelled, nothing written.")
        return 1


def main() -> int:
    # The Windows console is often cp1252 — player names can be anything
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        prog="fragsmith",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = ap.add_subparsers(dest="command", required=True)

    setup_p = sub.add_parser("setup", help="Answer a few questions and write config.toml")
    setup_p.set_defaults(func=cmd_setup)

    doc_p = sub.add_parser("doctor", help="Check that everything needed is installed and configured")
    doc_p.set_defaults(func=cmd_doctor)

    run_p = sub.add_parser("run", help="Steps 0-3 for one demo: parse, score, edit plan")
    run_p.add_argument("demo", help="Path to a .dem or .dem.bz2 file")
    run_p.add_argument("--player", help="SteamID64 for the POV (overrides config)")
    run_p.add_argument("--music", help="Music folder (overrides [music].music_dir)")
    run_p.add_argument(
        "--director", choices=("auto", "rules", "claude"),
        help="Who makes the edit plan (overrides [director].mode)",
    )
    run_p.set_defaults(func=cmd_run)

    ren_p = sub.add_parser(
        "render",
        help="Step 4: send the render job to the worker (THIS is the approval)",
    )
    ren_p.add_argument("run_dir", help="Path to runs/<id>/ from 'run'")
    ren_p.set_defaults(func=cmd_render)

    fin_p = sub.add_parser(
        "finish", help="Steps 5-7: beats + editing + assembly (needs raw clips)"
    )
    fin_p.add_argument("run_dir", help="Path to runs/<id>/ from 'run'")
    fin_p.add_argument("--clips", help="Raw clip folder (default: <run-dir>/03-raw-clips)")
    fin_p.add_argument("--music", help="Override the music choice (file name in the music folder)")
    fin_p.set_defaults(func=cmd_finish)

    batch_p = sub.add_parser("batch", help="Process every demo in a folder (steps 0-3)")
    batch_p.add_argument("folder", help="Folder with .dem/.dem.bz2 files")
    batch_p.add_argument("--player", help="SteamID64 for the POV (overrides config)")
    batch_p.add_argument("--force", action="store_true", help="Also re-run already processed demos")
    batch_p.add_argument(
        "--director", choices=("auto", "rules", "claude"),
        help="Who makes the edit plans (overrides [director].mode)",
    )
    batch_p.set_defaults(func=cmd_batch)

    clean_p = sub.add_parser("cleanup", help="Delete raw clips/segments for a run")
    clean_p.add_argument("run_dir", help="Path to runs/<id>/")
    clean_p.add_argument("--force", action="store_true", help="Delete even without final.mp4")
    clean_p.set_defaults(func=cmd_cleanup)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
