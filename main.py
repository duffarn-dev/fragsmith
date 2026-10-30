"""Fragsmith — CS2 .dem → fragmovie.

Bruk:
    python main.py run <demo.dem> [--player <steamid64>] [--music <fil|mappe>]

Fase (a): kjører steg 0-2 (learnings, parsing, hype-scoring) og stopper med
kandidatliste. Steg 3+ (Director, rendering, editing) kommer i fase b-f.
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
    """Enkel .env-laster (KEY=VALUE per linje); setter ikke over eksisterende."""
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
    """Steg 0: les learnings_runtime.md FØR alt annet (fast regel)."""
    path = PROJECT_DIR / "learnings_runtime.md"
    if not path.exists():
        print("ADVARSEL: learnings_runtime.md mangler!")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    entries = [l for l in lines if l.startswith("- 20")]
    print(f"Steg 0: leste learnings_runtime.md ({len(entries)} læringspunkt)")
    for entry in entries:
        print(f"    {entry}")


def load_config() -> dict:
    path = PROJECT_DIR / "config.toml"
    if not path.exists():
        print(f"FEIL: finner ikke {path}")
        print("Kopier config.example.toml til config.toml og fyll inn dine verdier.")
        sys.exit(1)
    with open(path, "rb") as fh:
        return tomllib.load(fh)


def _resolve_demo(path_arg: str) -> Path:
    """Godta .dem eller .dem.bz2 (Valve MM lagrer bz2) — pakk ut ved behov."""
    demo = Path(path_arg)
    if demo.suffix == ".bz2":
        target = demo.with_suffix("")
        if not target.exists():
            print(f"Pakker ut {demo.name} ...")
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
    print(f"Kjøring: runs/{run_id}/")

    # --- Steg 1: parse demo ---------------------------------------------
    print(f"Steg 1: parser {demo.name} ...")
    try:
        events = parse_demo(demo)
    except DemoParseError as exc:
        print(f"STOPP i steg 1: {exc}")
        (run_dir / "error.json").write_text(
            json.dumps({"step": 1, "error": str(exc)}, indent=2), encoding="utf-8"
        )
        return 1

    (run_dir / "00-events.json").write_text(
        json.dumps(events, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"    kart: {events['map'] or 'ukjent'} | "
        f"{len(events['rounds'])} runder | {len(events['kills'])} kills | "
        f"{len(events['players'])} spillere"
    )

    # --- Steg 2: hype-scoring ---------------------------------------------
    print("Steg 2: hype-scoring ...")
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
    print(f"    {len(candidates)} kandidatklipp:")
    print(f"    {'klipp':<22} {'type':<17} {'kills':>5} {'score':>6} {'sek':>6}")
    for c in candidates:
        print(
            f"    {c.clip_id:<22} {c.kind:<17} {c.kill_count:>5} "
            f"{c.hype_score:>6.2f} {c.duration_seconds:>6.1f}"
        )

    if flags:
        print("Flagg:")
        for f in flags:
            print(f"    ! {f}")

    if not candidates:
        print("Ingen kandidatklipp — stopper her (ingenting å lage film av).")
        return 0

    # --- Steg 3: AI Director ---------------------------------------------
    import os

    from lib.director import DirectorError, list_music_files, run_director

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "STOPP før steg 3: ANTHROPIC_API_KEY mangler. "
            "Lim nøkkelen inn i .env-fila i prosjektmappa."
        )
        return 1

    music_dir = args.music or cfg.get("music", {}).get("music_dir", "")
    music_files = list_music_files(music_dir)
    if not music_files:
        print(
            f"STOPP før steg 3: ingen musikkfiler funnet i '{music_dir}'. "
            f"Legg mp3/wav/flac der, eller pek med --music."
        )
        return 1

    dcfg = cfg.get("director", {})
    print(f"Steg 3: AI Director ({dcfg.get('model', 'claude-opus-4-8')}) ...")
    try:
        plan = run_director(
            candidates=candidates,
            events=events,
            music_files=music_files,
            pov_name=pov_name,
            run_dir=run_dir,
            model=dcfg.get("model", "claude-opus-4-8"),
            max_retries=int(dcfg.get("max_retries", 3)),
        )
    except DirectorError as exc:
        print(f"STOPP i steg 3: {exc}")
        (run_dir / "error.json").write_text(
            json.dumps({"step": 3, "error": str(exc)}, indent=2), encoding="utf-8"
        )
        return 1

    (run_dir / "02-edit-plan.json").write_text(
        plan.model_dump_json(indent=2), encoding="utf-8"
    )

    # --- FASESTOPP: vis edit-plan + estimater, vent på godkjenning ---------
    from lib.edit_engine import pick_replay_clip, wants_high_fps

    by_id = {c.clip_id: c for c in candidates}
    chosen = sorted(plan.decisions, key=lambda d: d.order)
    total_sec = sum(by_id[d.clip_id].duration_seconds for d in chosen)

    # P20: klipp med slow-mo rendres i høy fps — samme utvalg som steg 4 gjør
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

    # Grove estimat: rendering ~2x sanntid + oppstart; råklipp ~40 MB/min
    # 1080p60. Høy-fps-klipp skalerer ~lineært med fps (flere frames), og
    # to pass betyr én ekstra CS2-oppstart (~2 min).
    fps_scale = high_fps / base_fps if high_fps else 1.0
    est_render_min = (
        total_sec * 2 + high_sec * 2 * (fps_scale - 1)
        + 120 + (120 if highfps_ids else 0)
    ) / 60
    est_disk_mb = (total_sec + high_sec * (fps_scale - 1)) / 60 * 40

    print()
    print("=" * 64)
    print("EDIT-PLAN (fasestopp — rendering krever godkjenning)")
    print(f"  Musikk: {plan.music_track}")
    print(f"  Intro:  {plan.intro_text}   Outro: {plan.outro_text}")
    print(f"  {'#':>2} {'klipp':<22} {'score':>6} {'sek':>6}  overlay/slow-mo")
    for d in chosen:
        c = by_id[d.clip_id]
        extras = []
        if d.overlay_text:
            extras.append(f'"{d.overlay_text}"')
        if d.slowmo_segments:
            extras.append(f"{len(d.slowmo_segments)} slow-mo")
        if d.clip_id in highfps_ids:
            extras.append(f"{high_fps} fps (P20)")
        print(
            f"  {d.order:>2} {d.clip_id:<22} {c.hype_score:>6.2f} "
            f"{c.duration_seconds:>6.1f}  {', '.join(extras)}"
        )
    if plan.dropped_clip_ids:
        print(f"  Droppet: {', '.join(plan.dropped_clip_ids)}")
    if highfps_ids:
        print(
            f"  P20: {len(highfps_ids)} klipp rendres i {high_fps} fps "
            f"(ekte smooth slow-mo) — rendering går i 2 pass."
        )
    print(
        f"  Total klippetid: {total_sec:.0f} s | grovt estimat rendering: "
        f"~{est_render_min:.0f} min | råklipp-disk: ~{est_disk_mb:.0f} MB"
    )
    print("=" * 64)
    print("Begrunnelser per klipp ligger i 02-edit-plan.json (reasoning).")
    print("Rendering (steg 4) kjøres på render-PC-en — kommer i fase (c).")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Steg 4: send render-jobb til worker på render-PC-en.

    Å kjøre denne kommandoen ER godkjenningen fra fasestoppet — den legger
    jobben i køen som starter CS2-rendering på render-PC-en.
    """
    read_learnings()
    cfg = load_config()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    run_id = run_dir.name
    plan_path = run_dir / "02-edit-plan.json"
    if not plan_path.exists():
        print(f"FEIL: finner ikke {plan_path} — kjør 'run' først.")
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
        print(
            f"P20: {len(high)} klipp rendres i {high[0]['framerate']} fps "
            f"({', '.join(c['clip_id'] for c in high)}) — workeren kjører "
            f"2 render-pass (én ekstra CS2-oppstart)."
        )
    n_alt = sum(1 for c in job["clips"] if c.get("alt_pov_steamid"))
    if n_alt:
        alt = next(c for c in job["clips"] if c.get("alt_pov_steamid"))
        print(
            f"P19: {alt['clip_id']} får EKSTRA offer-POV-sekvens "
            f"({alt.get('alt_pov_name', '?')}) for instant replay — "
            f"litt mer render-tid og disk."
        )

    demo_path = Path(plan.demo_path)
    if not demo_path.exists():
        print(f"FEIL: finner ikke demofila {demo_path} (trengs for kopiering).")
        return 1

    render_cfg = cfg.get("render", {})
    if render_cfg.get("ssh_host"):
        from lib.render_clips import SshError, submit_job_ssh

        print(f"Sender jobb via SSH til {render_cfg['ssh_host']} ...")
        try:
            job_ref = submit_job_ssh(job, demo_path, render_cfg)
        except SshError as exc:
            print(f"STOPP: {exc}")
            return 1
        print(f"Jobb lagt i kø: {job_ref}")
        print(f"{len(job['clips']) + n_alt} sekvenser renderes når workeren plukker jobben.")
        print("Start workeren på render-PC-en hvis den ikke kjører "
              "(dobbeltklikk C:\\fragsmith-jobs\\start-worker.cmd).")
        print(f"Når ferdig: python main.py finish {args.run_dir}")
        return 0

    job_dir_cfg = render_cfg.get("job_dir", "")
    if not job_dir_cfg:
        # Ingen delt mappe konfigurert — skriv jobben lokalt for manuell (USB-)flytting
        job_path = run_dir / f"{run_id}.render-job.json"
        job_path.write_text(
            json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"Jobbfil skrevet: {job_path}")
        print("render.job_dir er ikke satt i config.toml — manuell overføring:")
        print(f"  1. Kopier jobbfila til <jobbmappe>/inbox/ på render-PC-en")
        print(f"  2. Kopier {demo_path.name} til <jobbmappe>/demos/")
        print(f"  3. Kjør worker.py der: python worker.py --job-dir <jobbmappe>")
        print(f"  4. Kopier <jobbmappe>/outbox/{run_id}/ hit som 03-raw-clips/")
        return 0

    job_dir = Path(job_dir_cfg)
    print(f"Sender jobb til kø: {job_dir}")
    job_path = submit_job(job, demo_path, job_dir)
    print(f"Jobb lagt i kø: {job_path}")
    print(f"{len(job['clips']) + n_alt} sekvenser renderes når workeren plukker jobben.")
    print(f"Når ferdig: python main.py finish {args.run_dir}")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    """Steg 5-7: beats + editing + assembly. Krever råklipp (steg 4) i
    <run-dir>/03-raw-clips/ (eller --clips)."""
    read_learnings()
    cfg = load_config()

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    plan_path = run_dir / "02-edit-plan.json"
    cand_path = run_dir / "01-candidates.json"
    if not plan_path.exists():
        print(f"FEIL: finner ikke {plan_path} — kjør 'run' først.")
        return 1

    from lib.assemble import assemble, build_intro_hook, write_report
    from lib.beats import analyze_beats
    from lib.edit_engine import build_segments, plan_drop_sync
    from lib.ffmpeg_util import FfmpegError, probe_duration
    from lib.models import EditPlan, Highlight

    plan = EditPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    if args.music:
        plan.music_track = args.music  # overstyr Directors musikkvalg
    cand_data = json.loads(cand_path.read_text(encoding="utf-8"))
    candidates_by_id = {
        c["clip_id"]: Highlight.model_validate(c) for c in cand_data["candidates"]
    }

    # Demo-kontekst for P7 (kart i lowerthird) og P13 (stats-outro)
    events: dict = {}
    events_path = run_dir / "00-events.json"
    if events_path.exists():
        events = json.loads(events_path.read_text(encoding="utf-8"))

    clips_dir = Path(args.clips) if args.clips else run_dir / "03-raw-clips"
    if not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4")):
        render_cfg = cfg.get("render", {})
        # Hent automatisk via SSH hvis konfigurert
        if render_cfg.get("ssh_host"):
            from lib.render_clips import collect_clips_ssh

            copied, result = collect_clips_ssh(
                run_dir.name, render_cfg, run_dir / "03-raw-clips"
            )
            if copied:
                print(f"Hentet {copied} råklipp fra render-PC-en via SSH.")
                clips_dir = run_dir / "03-raw-clips"
                for w in result.get("warnings", []):
                    print(f"    ! worker-advarsel: {w}")
            elif result:
                print(
                    f"Render-jobben har status '{result.get('status')}'"
                    + (f": {result.get('error')}" if result.get("error") else "")
                )
            else:
                print("Ingen result.json fra workeren ennå — jobben er ikke ferdig.")
        # Ellers: hent fra delt/lokal jobbmappe hvis konfigurert
        job_dir_cfg = render_cfg.get("job_dir", "")
        if job_dir_cfg and (not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4"))):
            from lib.render_clips import collect_clips

            copied, result = collect_clips(
                run_dir.name, Path(job_dir_cfg), run_dir / "03-raw-clips"
            )
            if copied:
                print(f"Hentet {copied} råklipp fra render-køens outbox.")
                clips_dir = run_dir / "03-raw-clips"
                for w in result.get("warnings", []):
                    print(f"    ! worker-advarsel: {w}")
            elif result:
                print(
                    f"Render-jobben har status '{result.get('status')}'"
                    + (f": {result.get('error')}" if result.get("error") else "")
                )
    if not clips_dir.is_dir() or not any(clips_dir.glob("*.mp4")):
        print(
            f"FEIL: ingen råklipp i {clips_dir}. Kjør 'render' og vent på "
            f"workeren, eller pek med --clips."
        )
        return 1

    music_dir = Path(cfg.get("music", {}).get("music_dir", ""))
    music_path = Path(plan.music_track)
    if not music_path.is_absolute():
        music_path = music_dir / plan.music_track
    if not music_path.exists():
        print(f"FEIL: finner ikke musikkspor: {music_path}")
        return 1

    log_path = run_dir / "ffmpeg.log"

    print(f"Steg 5: beat-analyse av {music_path.name} ...")
    beats = analyze_beats(music_path)
    (run_dir / "05-beats.json").write_text(
        json.dumps(beats, indent=2), encoding="utf-8"
    )
    print(f"    {beats['bpm']} BPM, {len(beats['beat_times'])} beats")
    if beats.get("drops"):
        drop_str = ", ".join(f"{d['time_s']:.1f}s" for d in beats["drops"])
        print(f"    drops funnet: {drop_str} (sterkest først)")

    # Cold open-glimtene ligger FØR introen på tidslinja — de må bygges nå
    # så steg 6 vet hvor segmentene faktisk starter (beat-sync-bug fikset
    # 2026-07-07; glimtene ble før ikke regnet med).
    work_dir = run_dir / "04-segments"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        hooks = build_intro_hook(
            plan, candidates_by_id, clips_dir, work_dir, cfg, log_path
        )
    except FfmpegError as exc:
        print(f"STOPP i steg 6 (cold open): {exc}")
        return 1
    hook_s = sum(probe_duration(h) for h in hooks)

    # P21: legg klimaks-klippet på musikkens drop hvis mulig
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
            f"intro-kortet justert til {intro_s:.1f} s"
            if drop_info["method"] == "intro"
            else f"musikken starter {music_offset:.1f} s ut i sporet"
        )
        print(
            f"    P21: klimaks ({drop_info['climax_clip_id']}) legges på "
            f"droppet ved {drop_info['drop_track_s']:.1f} s — {method}"
        )
    elif editing.get("drop_sync", False):
        why = (
            "ingen drop-kandidat kunne treffes"
            if beats.get("drops")
            else "ingen tydelig drop i sporet"
        )
        print(f"    P21: {why} — klimaks-til-slutt som før.")

    print("Steg 6: editing engine (slow-mo, overlays, beat-justering) ...")
    try:
        segments, cut_log, flags = build_segments(
            plan, candidates_by_id, clips_dir,
            work_dir, beats_used, cfg, log_path,
            context={"map": events.get("map", "")},
            timeline_start=hook_s + intro_s,
        )
    except FfmpegError as exc:
        print(f"STOPP i steg 6: {exc}")
        return 1
    if not segments:
        print("STOPP: ingen segmenter kunne bygges (mangler alle råklipp?).")
        return 1
    print(f"    {len(segments)} segmenter bygd")

    # P21: mål FAKTISK avvik mot droppet (encoding gir frame-granularitet)
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
            print(f"    P21: klimaks lander {err_ms:+d} ms fra droppet")
        beats_used = dict(beats_used)
        beats_used["drop_sync"] = {
            k: v for k, v in drop_info.items() if k != "beats"
        }

    print("Steg 7: assembly (intro, musikk-miks, eksport) ...")
    try:
        final = assemble(
            plan, segments, music_path, run_dir, cfg, log_path,
            candidates_by_id=candidates_by_id, clips_dir=clips_dir,
            events=events, cut_log=cut_log,
            hook_files=hooks, intro_seconds=intro_s,
            music_offset=music_offset,
        )
    except FfmpegError as exc:
        print(f"STOPP i steg 7: {exc}")
        return 1

    report = write_report(
        plan, candidates_by_id, cut_log, flags, beats_used, final, run_dir,
        events=events,
    )

    if flags:
        print("Flagg:")
        for f in flags:
            print(f"    ! {f}")
    print()
    print(f"FERDIG: {final}")
    print(f"Rapport: {report}")
    print("Råklipp beholdes til du har sett filmen — slett med --cleanup senere.")
    return 0


def cmd_batch(args: argparse.Namespace) -> int:
    """Fase f: prosesser alle demoer i en mappe sekvensielt (steg 0-3).

    processed.json husker hva som er kjørt, så batch kan gjenopptas uten
    dobbeltarbeid. Rendering skjer ALDRI parallelt — worker på render-PC-en
    tar jobbene sekvensielt (fase c).
    """
    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"FEIL: {folder} er ikke en mappe.")
        return 1

    processed_path = PROJECT_DIR / "processed.json"
    processed: dict = {}
    if processed_path.exists():
        processed = json.loads(processed_path.read_text(encoding="utf-8"))

    # .dem foretrekkes; .bz2 tas med der .dem ikke alt er pakket ut
    demos: dict[str, Path] = {}
    for f in sorted(folder.iterdir()):
        if f.suffix == ".dem":
            demos[f.stem] = f
        elif f.name.endswith(".dem.bz2"):
            demos.setdefault(f.name[: -len(".dem.bz2")], f)

    todo = {k: v for k, v in demos.items() if args.force or k not in processed}
    print(f"Batch: {len(demos)} demoer funnet, {len(todo)} uprosesserte.")

    failed = 0
    for i, (stem, path) in enumerate(todo.items(), start=1):
        print(f"\n--- [{i}/{len(todo)}] {path.name} " + "-" * 20)
        code = cmd_run(
            argparse.Namespace(demo=str(path), player=args.player, music=None)
        )
        processed[stem] = {
            "demo": path.name,
            "status": "ok" if code == 0 else "feil",
            "processed_at": datetime.now().isoformat(timespec="seconds"),
        }
        processed_path.write_text(
            json.dumps(processed, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        failed += code != 0

    print(f"\nBatch ferdig: {len(todo) - failed} ok, {failed} feilet.")
    print("Neste: godkjenn edit-planene og render på render-PC-en (fase c).")
    return 1 if failed else 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Slett temp-filer (råklipp + segmenter) for en kjøring. Beholder
    final.mp4, report.md og alle json-artefakter."""
    import shutil

    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = PROJECT_DIR / run_dir
    if not (run_dir / "final.mp4").exists() and not args.force:
        print(
            "STOPP: final.mp4 finnes ikke i denne kjøringen — sletting av "
            "råklipp nå betyr ny rendering. Bruk --force hvis du er sikker."
        )
        return 1
    freed = 0
    for sub in ("03-raw-clips", "04-segments"):
        target = run_dir / sub
        if target.is_dir():
            freed += sum(f.stat().st_size for f in target.rglob("*") if f.is_file())
            shutil.rmtree(target)
            print(f"Slettet {sub}/")
    print(f"Frigjort ~{freed / 1e6:.0f} MB.")
    return 0


def main() -> int:
    # Windows-konsollen er ofte cp1252 — spillernavn kan være hva som helst
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(prog="fragsmith", description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Prosesser én demo")
    run_p.add_argument("demo", help="Sti til .dem-fil")
    run_p.add_argument("--player", help="SteamID64 for POV (overstyrer config)")
    run_p.add_argument("--music", help="Musikkfil/-mappe (brukes fra fase d)")
    run_p.set_defaults(func=cmd_run)

    ren_p = sub.add_parser(
        "render",
        help="Steg 4: send render-jobb til worker (DETTE er godkjenningen)",
    )
    ren_p.add_argument("run_dir", help="Sti til runs/<id>/ fra 'run'")
    ren_p.set_defaults(func=cmd_render)

    fin_p = sub.add_parser(
        "finish", help="Steg 5-7: beats + editing + assembly (krever råklipp)"
    )
    fin_p.add_argument("run_dir", help="Sti til runs/<id>/ fra 'run'")
    fin_p.add_argument("--clips", help="Råklipp-mappe (default: <run-dir>/03-raw-clips)")
    fin_p.add_argument("--music", help="Overstyr musikkvalg (filnavn i musikkmappa)")
    fin_p.set_defaults(func=cmd_finish)

    batch_p = sub.add_parser("batch", help="Prosesser alle demoer i en mappe (steg 0-3)")
    batch_p.add_argument("folder", help="Mappe med .dem/.dem.bz2-filer")
    batch_p.add_argument("--player", help="SteamID64 for POV (overstyrer config)")
    batch_p.add_argument("--force", action="store_true", help="Kjør også allerede prosesserte")
    batch_p.set_defaults(func=cmd_batch)

    clean_p = sub.add_parser("cleanup", help="Slett råklipp/segmenter for en kjøring")
    clean_p.add_argument("run_dir", help="Sti til runs/<id>/")
    clean_p.add_argument("--force", action="store_true", help="Slett selv uten final.mp4")
    clean_p.set_defaults(func=cmd_cleanup)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
