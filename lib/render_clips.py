"""Steg 4 (dev-siden): bygg render-jobber for worker på render-PC-en.

Jobbkø over delt mappe (eller USB): dev-PC-en skriver en jobb-JSON +
demofil, workeren (worker.py) plukker den, kjører csdm analyze + video
(HLAE + FFmpeg, alle sekvenser i ÉN CS2-oppstart), og legger råklippene
i outbox. Deretter: `python main.py finish <run-dir>` henter dem derfra.

Jobbmappe-struktur:
    <job_dir>/demos/    demofiler
    <job_dir>/inbox/    <run_id>.json — jobber som venter
    <job_dir>/working/  jobb under arbeid (flyttet av worker)
    <job_dir>/outbox/   <run_id>/<clip_id>.mp4 + <run_id>.result.json
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from lib.edit_engine import pick_replay_clip, wants_high_fps
from lib.models import EditPlan, Highlight


def build_render_job(
    run_id: str,
    plan: EditPlan,
    candidates_by_id: dict[str, Highlight],
    pov_steamid: str,
    pov_name: str,
    cfg: dict,
) -> dict:
    video = cfg.get("video", {})
    render_cfg = cfg.get("render", {})
    fx = cfg.get("effects", {})
    # P17/P19: replay-klippet får en EKSTRA sekvens fra offerets POV.
    # Utvalget MÅ matche edit_engine.pick_replay_clip (samme funksjon).
    replay = pick_replay_clip(plan, candidates_by_id, cfg)
    # P20: klipp med slow-mo rendres i høy fps → ekte smooth nedbremsing.
    # csdm har GLOBALT framerate (verifisert i kilden 2026-07-07), så
    # workeren deler jobben i ett pass per framerate (egen CS2-oppstart).
    base_fps = int(video.get("framerate", 60))
    high_fps = int(render_cfg.get("highfps_framerate", 0))
    if high_fps <= base_fps:
        high_fps = 0  # av (eller feilkonfigurert)
    clips = []
    for d in sorted(plan.decisions, key=lambda d: d.order):
        cand = candidates_by_id[d.clip_id]
        # P2: X-ray kun på høydepunkt-klipp (ace/clutch) og AWP-drap —
        # X-ray på alt ser rotete ut (spec-en, edge cases)
        show_xray = cand.kind in ("ace", "clutch") or any(
            "awp" in w.lower() for w in cand.weapons
        )
        clip = {
            "clip_id": d.clip_id,
            "number": d.order,  # csdm-sekvensnummer
            "start_tick": cand.start_tick,
            "end_tick": cand.end_tick,
            "show_xray": show_xray,
        }
        if replay and replay[0] == d.clip_id and replay[1].victim_steamid:
            clip["alt_pov_steamid"] = replay[1].victim_steamid
            clip["alt_pov_name"] = replay[1].victim_name
        # P20: replay-klippet bremser alltid alt-vinkelen (0.45×) — det og
        # begge sekvensene dets skal i høy fps, i tillegg til slow-mo-klipp
        if high_fps and (
            wants_high_fps(d, cand, fx) or (replay and replay[0] == d.clip_id)
        ):
            clip["framerate"] = high_fps
        clips.append(clip)
    return {
        "schema_version": "1.3",  # 1.1: + true_view/show_xray (P2) · 1.2: + alt_pov (P17) · 1.3: + framerate per klipp (P20)
        "run_id": run_id,
        "demo_filename": Path(plan.demo_path).name,
        "pov_steamid": pov_steamid,
        "pov_name": pov_name,
        "true_view": bool(render_cfg.get("true_view", False)),
        "video": {
            "width": int(video.get("width", 1920)),
            "height": int(video.get("height", 1080)),
            "framerate": int(video.get("framerate", 60)),
        },
        "clips": clips,
    }


def submit_job(job: dict, demo_path: Path, job_dir: Path) -> Path:
    """Legg jobben i køen: kopier demo til demos/, skriv JSON til inbox/."""
    for sub in ("demos", "inbox", "working", "outbox"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    demo_target = job_dir / "demos" / demo_path.name
    if not demo_target.exists():
        size_gb = demo_path.stat().st_size / 1e9
        print(f"    kopierer demo ({size_gb:.1f} GB) til {demo_target} ...")
        shutil.copy2(demo_path, demo_target)

    job_path = job_dir / "inbox" / f"{job['run_id']}.json"
    job_path.write_text(
        json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return job_path


# --- SSH-transport (brukes når [render].ssh_host er satt i config) ---------


class SshError(RuntimeError):
    pass


def _ssh_opts(render_cfg: dict) -> list[str]:
    opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    key = render_cfg.get("ssh_key", "")
    if key:
        opts += ["-i", key]
    return opts


def _run(cmd: list[str], description: str) -> str:
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        raise SshError(
            f"{description} feilet (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-500:]}"
        )
    return proc.stdout


def _remote_sftp_path(remote_dir: str, *parts: str) -> str:
    # Windows OpenSSH SFTP vil ha /C:/sti-form for absolutte stier
    p = "/".join([remote_dir.replace("\\", "/").rstrip("/"), *parts])
    return f"/{p}" if not p.startswith("/") else p


def submit_job_ssh(job: dict, demo_path: Path, render_cfg: dict) -> str:
    host = render_cfg["ssh_host"]
    remote_dir = render_cfg.get("remote_job_dir", "C:/fragsmith-jobs")
    opts = _ssh_opts(render_cfg)
    remote_win = remote_dir.replace("/", "\\")

    demo_check = _run(
        ["ssh", *opts, host,
         f'if exist "{remote_win}\\demos\\{demo_path.name}" (echo JA) else (echo NEI)'],
        "sjekk av demo på render-PC",
    )
    if "JA" not in demo_check:
        size_gb = demo_path.stat().st_size / 1e9
        print(f"    overfører demo ({size_gb:.1f} GB) til render-PC-en ...")
        _run(
            ["scp", *opts, str(demo_path),
             f"{host}:{_remote_sftp_path(remote_dir, 'demos', demo_path.name)}"],
            "demo-overføring",
        )

    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(json.dumps(job, indent=2, ensure_ascii=False))
        tmp_path = tmp.name
    job_name = f"{job['run_id']}.json"
    try:
        # .part først, så rename — workeren skal aldri se en halv fil
        _run(
            ["scp", *opts, tmp_path,
             f"{host}:{_remote_sftp_path(remote_dir, 'inbox', job_name + '.part')}"],
            "jobb-overføring",
        )
        _run(
            ["ssh", *opts, host,
             f'move /y "{remote_win}\\inbox\\{job_name}.part" '
             f'"{remote_win}\\inbox\\{job_name}" >nul'],
            "jobb-aktivering",
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    return f"{host}:{remote_dir}/inbox/{job_name}"


def collect_clips_ssh(
    run_id: str, render_cfg: dict, target_dir: Path
) -> tuple[int, dict]:
    host = render_cfg["ssh_host"]
    remote_dir = render_cfg.get("remote_job_dir", "C:/fragsmith-jobs")
    opts = _ssh_opts(render_cfg)
    remote_win = remote_dir.replace("/", "\\")

    result: dict = {}
    try:
        out = _run(
            ["ssh", *opts, host,
             f'type "{remote_win}\\outbox\\{run_id}.result.json" 2>nul'],
            "henting av result.json",
        )
        if out.strip():
            result = json.loads(out)
    except (SshError, json.JSONDecodeError):
        return 0, {}

    copied = 0
    if result.get("status") in ("ok", "delvis"):
        target_dir.mkdir(parents=True, exist_ok=True)
        _run(
            ["scp", *opts,
             f"{host}:{_remote_sftp_path(remote_dir, 'outbox', run_id, '*.mp4')}",
             str(target_dir)],
            "henting av råklipp",
        )
        copied = len(list(target_dir.glob("*.mp4")))
    return copied, result


def collect_clips(run_id: str, job_dir: Path, target_dir: Path) -> tuple[int, dict]:
    """Hent ferdige råklipp fra outbox til runs/<id>/03-raw-clips/.

    Returnerer (antall kopiert, result-json fra worker eller {}).
    """
    outbox = job_dir / "outbox" / run_id
    result_path = job_dir / "outbox" / f"{run_id}.result.json"
    result = {}
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))

    copied = 0
    if outbox.is_dir():
        target_dir.mkdir(parents=True, exist_ok=True)
        for clip in outbox.glob("*.mp4"):
            shutil.copy2(clip, target_dir / clip.name)
            copied += 1
    return copied, result
