"""Step 4 (main-PC side): build render jobs for the worker on the render PC.

Job queue over a shared folder, SSH or USB: the main PC writes a job JSON +
demo file, the worker (worker.py) picks it up, runs csdm analyze + video
(HLAE + FFmpeg, all sequences in ONE CS2 launch), and puts the raw clips
in the outbox. Then: `python main.py finish <run-dir>` fetches them.

Job folder layout:
    <job_dir>/demos/    demo files
    <job_dir>/inbox/    <run_id>.json — jobs waiting
    <job_dir>/working/  job in progress (moved there by the worker)
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
    # Instant replay: the replay clip gets an EXTRA sequence from the victim's
    # POV. The selection MUST match edit_engine.pick_replay_clip (same function).
    replay = pick_replay_clip(plan, candidates_by_id, cfg)
    # Clips with slow-mo are rendered at high fps → real smooth slow-down.
    # csdm has a GLOBAL framerate, so the worker splits the job into one
    # pass per framerate (separate CS2 launch).
    base_fps = int(video.get("framerate", 60))
    high_fps = int(render_cfg.get("highfps_framerate", 0))
    if high_fps <= base_fps:
        high_fps = 0  # off (or misconfigured)
    clips = []
    for d in sorted(plan.decisions, key=lambda d: d.order):
        cand = candidates_by_id[d.clip_id]
        # X-ray only on highlight clips (ace/clutch) and AWP kills —
        # X-ray on everything looks cluttered
        show_xray = cand.kind in ("ace", "clutch") or any(
            "awp" in w.lower() for w in cand.weapons
        )
        clip = {
            "clip_id": d.clip_id,
            "number": d.order,  # csdm sequence number
            "start_tick": cand.start_tick,
            "end_tick": cand.end_tick,
            "show_xray": show_xray,
        }
        if replay and replay[0] == d.clip_id and replay[1].victim_steamid:
            clip["alt_pov_steamid"] = replay[1].victim_steamid
            clip["alt_pov_name"] = replay[1].victim_name
        # The replay clip always slows down the alternate angle (0.45×) — it
        # and both its sequences go to high fps, in addition to slow-mo clips
        if high_fps and (
            wants_high_fps(d, cand, fx) or (replay and replay[0] == d.clip_id)
        ):
            clip["framerate"] = high_fps
        clips.append(clip)
    return {
        "schema_version": "1.3",  # 1.1: + true_view/show_xray · 1.2: + alt_pov · 1.3: + framerate per clip
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
    """Queue the job: copy the demo to demos/, write the JSON to inbox/."""
    for sub in ("demos", "inbox", "working", "outbox"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    demo_target = job_dir / "demos" / demo_path.name
    if not demo_target.exists():
        size_gb = demo_path.stat().st_size / 1e9
        print(f"    copying demo ({size_gb:.1f} GB) to {demo_target} ...")
        shutil.copy2(demo_path, demo_target)

    job_path = job_dir / "inbox" / f"{job['run_id']}.json"
    job_path.write_text(
        json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return job_path


# --- SSH transport (used when [render].ssh_host is set in config) ----------


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
            f"{description} failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[-500:]}"
        )
    return proc.stdout


def _remote_sftp_path(remote_dir: str, *parts: str) -> str:
    # Windows OpenSSH SFTP wants the /C:/path form for absolute paths
    p = "/".join([remote_dir.replace("\\", "/").rstrip("/"), *parts])
    return f"/{p}" if not p.startswith("/") else p


def submit_job_ssh(job: dict, demo_path: Path, render_cfg: dict) -> str:
    host = render_cfg["ssh_host"]
    remote_dir = render_cfg.get("remote_job_dir", "C:/fragsmith-jobs")
    opts = _ssh_opts(render_cfg)
    remote_win = remote_dir.replace("/", "\\")

    demo_check = _run(
        ["ssh", *opts, host,
         f'if exist "{remote_win}\\demos\\{demo_path.name}" (echo YES) else (echo NO)'],
        "checking for the demo on the render PC",
    )
    if "YES" not in demo_check:
        size_gb = demo_path.stat().st_size / 1e9
        print(f"    transferring demo ({size_gb:.1f} GB) to the render PC ...")
        _run(
            ["scp", *opts, str(demo_path),
             f"{host}:{_remote_sftp_path(remote_dir, 'demos', demo_path.name)}"],
            "demo transfer",
        )

    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(json.dumps(job, indent=2, ensure_ascii=False))
        tmp_path = tmp.name
    job_name = f"{job['run_id']}.json"
    try:
        # .part first, then rename — the worker must never see a half-written file
        _run(
            ["scp", *opts, tmp_path,
             f"{host}:{_remote_sftp_path(remote_dir, 'inbox', job_name + '.part')}"],
            "job transfer",
        )
        _run(
            ["ssh", *opts, host,
             f'move /y "{remote_win}\\inbox\\{job_name}.part" '
             f'"{remote_win}\\inbox\\{job_name}" >nul'],
            "job activation",
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
            "fetching result.json",
        )
        if out.strip():
            result = json.loads(out)
    except (SshError, json.JSONDecodeError):
        return 0, {}

    copied = 0
    if result.get("status") in ("ok", "partial"):
        target_dir.mkdir(parents=True, exist_ok=True)
        _run(
            ["scp", *opts,
             f"{host}:{_remote_sftp_path(remote_dir, 'outbox', run_id, '*.mp4')}",
             str(target_dir)],
            "fetching raw clips",
        )
        copied = len(list(target_dir.glob("*.mp4")))
    return copied, result


def collect_clips(run_id: str, job_dir: Path, target_dir: Path) -> tuple[int, dict]:
    """Fetch finished raw clips from the outbox to runs/<id>/03-raw-clips/.

    Returns (number copied, result json from the worker or {}).
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
