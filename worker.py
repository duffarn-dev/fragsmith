"""Fragsmith render worker — runs on the RENDER PC (with CS2 + CS Demo Manager).

Only needed in two-PC mode ([render].mode = "ssh" or "folder"). With one PC
("local"), `main.py render` calls render_job() directly instead.

Standard library only: just needs Python 3.10+ installed.

    python worker.py --job-dir <shared folder or local queue> [--csdm <path to csdm>] [--once]

Picks up jobs from <job_dir>/inbox/ and, per job:
  1. csdm analyze <demo>       (skips demos already in the database)
  2. csdm video --config-file  (HLAE + FFmpeg; ALL clips in one CS2 launch)
  3. Renames the output files to <clip_id>.mp4 in <job_dir>/outbox/<run_id>/

IMPORTANT:
- The PC must be logged in with a desktop session — CS2 launches and is in
  use during rendering. Don't touch the machine while it runs.
- Rendering never happens in parallel: one job, one sequence at a time.
- Two job failures in a row → the worker stops itself (usually a sign that a
  game update broke HLAE; update CS Demo Manager and try again).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# The CLI is csdm.cmd (not .exe) in the install folder
DEFAULT_CSDM = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs" / "cs-demo-manager" / "csdm.cmd"
)
POLL_SECONDS = 10
# CS Demo Manager's desktop app and `csdm video` both serve CS2 on this port
CSDM_GAME_PORT = 4574


def csdm_app_running() -> bool:
    """True when CS Demo Manager's desktop app is open. It holds the port
    CS2 connects to, so CS2 talks to the app and `csdm video` waits forever
    without rendering anything (seen 2026-10-31: 25 min, zero output)."""
    try:
        with socket.create_connection(("localhost", CSDM_GAME_PORT), timeout=1):
            return True
    except OSError:
        return False


def find_cs2() -> Path | None:
    """CS2's install folder via Steam's library list (Windows only)."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
    except (ImportError, OSError):
        return None
    libraries = [steam]
    vdf = steam / "steamapps" / "libraryfolders.vdf"
    if vdf.exists():
        text = vdf.read_text(encoding="utf-8", errors="replace")
        libraries += [
            Path(p.replace("\\\\", "\\"))
            for p in re.findall(r'"path"\s+"([^"]+)"', text)
        ]
    for lib in libraries:
        if (lib / "steamapps" / "appmanifest_730.acf").exists():
            return lib / "steamapps" / "common" / "Counter-Strike Global Offensive"
    return None


def read_steam_inf(cs2_dir: Path) -> dict[str, str]:
    """game/csgo/steam.inf as a dict: PatchVersion (1.41.9.4, the number Steam
    and demo headers use), ClientVersion, VersionDate ..."""
    inf = cs2_dir / "game" / "csgo" / "steam.inf"
    if not inf.exists():
        return {}
    pairs = (line.split("=", 1) for line in inf.read_text(errors="replace").splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in pairs}


def file_version(path: Path) -> str | None:
    """The Windows file version of an .exe ("2.192.10.0"), or None."""
    if os.name != "nt" or not path.exists():
        return None
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("version")
    size = api.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    if not api.GetFileVersionInfoW(str(path), 0, size, buf):
        return None
    ptr, length = ctypes.c_void_p(), wintypes.UINT()
    if not api.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(length)):
        return None
    # VS_FIXEDFILEINFO: signature, struct version, file version MS, file version LS
    info = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32 * 4)).contents
    ms, ls = info[2], info[3]
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"


def detect_versions(csdm: Path) -> dict:
    """CS2, HLAE and CS Demo Manager on this PC (None when not found). Goes
    into every render result, so `main.py selftest` can compare them with
    compat/tested-versions.json, whichever PC renders."""
    def three(v: str | None) -> str | None:  # "2.192.10.0" → "2.192.10"
        return ".".join(v.split(".")[:3]) if v else None

    versions: dict = {"cs2": None, "cs2_client": None, "cs2_date": None,
                      "hlae": None, "cs_demo_manager": None}
    try:
        cs2 = find_cs2()
        inf = read_steam_inf(cs2) if cs2 else {}
        versions.update(cs2=inf.get("PatchVersion"), cs2_client=inf.get("ClientVersion"),
                        cs2_date=inf.get("VersionDate"))
        hlae = Path(os.environ.get("USERPROFILE", "")) / ".csdm" / "hlae" / "HLAE.exe"
        versions["hlae"] = three(file_version(hlae))
        versions["cs_demo_manager"] = three(file_version(csdm.parent / "cs-demo-manager.exe"))
    except Exception as exc:  # noqa: BLE001 — versions are a nice-to-have, never a failure
        versions["error"] = str(exc)
    return versions


def log(msg: str, job_dir: Path | None = None) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if job_dir is not None:
        with open(job_dir / "worker.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run_csdm(csdm: Path, args: list[str], job_dir: Path) -> tuple[int, str]:
    cmd = [str(csdm), *args]
    log(f"running: {subprocess.list2cmdline(cmd)}", job_dir)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        if line.strip():
            log(f"  csdm: {line.strip()}", job_dir)
    return proc.returncode, output


ALT_SEQ_OFFSET = 100  # alternate-POV sequences are numbered base + 100


def build_csdm_configs(job: dict, demo_path: Path, raw_dir: Path) -> list[dict]:
    """One csdm config per framerate in the job. csdm has a GLOBAL
    framerate — clips with their own "framerate" field (schema 1.3, slow-mo
    clips at high fps) are therefore grouped into a separate pass with its
    own CS2 launch. Older jobs (without the field) give exactly one config.
    Sequence numbers are unique across passes, so the output mapping
    (sequence-N-...) is unaffected."""
    video = job["video"]
    base_fps = int(video["framerate"])
    by_fps: dict[int, list[dict]] = {}
    for clip in job["clips"]:
        fps = int(clip.get("framerate", base_fps))
        seq = {
            "number": clip["number"],
            "startTick": clip["start_tick"],
            "endTick": clip["end_tick"],
            # Per-clip X-ray from the main PC (ace/clutch/AWP);
            # older jobs (schema 1.0) lack the field → off
            "showXRay": bool(clip.get("show_xray", False)),
            "showAssists": True,
            "showOnlyDeathNotices": True,
            "playersOptions": [],
            "cameras": [],
            "recordAudio": True,
            "playerVoicesEnabled": False,
            "deathNoticesDuration": 5,
            "playerCameras": [
                {
                    "tick": clip["start_tick"],
                    "playerSteamId": job["pov_steamid"],
                    "playerName": job.get("pov_name", ""),
                }
            ],
        }
        by_fps.setdefault(fps, []).append(seq)
        # Extra sequence with the same tick range from the VICTIM's POV
        # (instant replay) — inherits the clip's framerate (the replay
        # slows down exactly this angle). Older jobs: no extra sequence.
        if clip.get("alt_pov_steamid"):
            by_fps[fps].append(
                {
                    **seq,
                    "number": clip["number"] + ALT_SEQ_OFFSET,
                    "showXRay": False,
                    "playerCameras": [
                        {
                            "tick": clip["start_tick"],
                            "playerSteamId": clip["alt_pov_steamid"],
                            "playerName": clip.get("alt_pov_name", ""),
                        }
                    ],
                }
            )
    return [
        {
            "demoPath": str(demo_path),
            "outputFolderPath": str(raw_dir),
            "recordingSystem": "HLAE",
            "recordingOutput": "video",
            "encoderSoftware": "FFmpeg",
            "framerate": fps,
            "width": video["width"],
            "height": video["height"],
            "closeGameAfterRecording": True,
            "concatenateSequences": False,
            # Correct POV body/arms. Can in rare cases hide the crosshair —
            # verify per CS2 version (learnings_runtime.md)
            "trueView": bool(job.get("true_view", False)),
            # Explicit — csdm's DEFAULT container is AVI (+ mp3 audio,
            # crf 23); we want predictable mp4/aac
            "ffmpegSettings": {
                "customExecutableLocation": "",
                "audioBitrate": 256,
                "constantRateFactor": 18,
                "videoCodec": "libx264",
                "audioCodec": "aac",
                "videoContainer": "mp4",
                "inputParameters": "",
                "outputParameters": "",
            },
            "sequences": seqs,
        }
        for fps, seqs in sorted(by_fps.items())  # base-fps pass first
    ]


VIDEO_EXTS = {".mp4", ".avi", ".mkv"}


def map_outputs(raw_dir: Path, clips: list[dict]) -> tuple[dict[str, Path], list[str]]:
    """Map produced video files to clip_id via csdm's naming pattern:
    sequence-<N>-tick-<start>-to-<end>.<container>  (get-sequence-output-file-path.ts)
    """
    produced = sorted(
        p for p in raw_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS
    )
    warnings: list[str] = []
    by_number: dict[int, Path] = {}
    for f in produced:
        m = re.match(r"sequence-(\d+)-", f.name)
        if m:
            by_number[int(m.group(1))] = f

    mapping: dict[str, Path] = {}
    for clip in clips:
        if clip["number"] in by_number:
            mapping[clip["clip_id"]] = by_number[clip["number"]]

    if len(mapping) != len(clips) and len(produced) == len(clips):
        warnings.append(
            "File names didn't follow the sequence-N pattern — mapped in "
            "sorted order instead (check the clips!)."
        )
        mapping = {
            clip["clip_id"]: f
            for clip, f in zip(sorted(clips, key=lambda c: c["number"]), produced)
        }
    return mapping, warnings


def render_job(
    job: dict, demo_path: Path, out_dir: Path, csdm: Path,
    log_dir: Path, work_dir: Path,
) -> dict:
    """Render every clip in a job to out_dir/<clip_id>.mp4 and return the
    result dict (status ok/partial/failed, per-clip status, warnings).

    Used by the queue (process_job) and directly by `main.py render` in
    one-PC mode. Raises RuntimeError when csdm fails outright.
    """
    run_id = job["run_id"]
    result: dict = {"run_id": run_id, "status": "failed", "warnings": [], "clips": {},
                    "versions": detect_versions(csdm)}
    if not demo_path.exists():
        raise RuntimeError(f"Demo file missing: {demo_path}")

    code, _ = run_csdm(csdm, ["analyze", str(demo_path)], log_dir)
    if code != 0:
        raise RuntimeError(f"csdm analyze failed (exit {code}).")
    if csdm_app_running():
        raise RuntimeError(
            "The CS Demo Manager app is open on this PC. Close it (check the "
            "system tray too) — while it runs, CS2 connects to the app and "
            "rendering never starts."
        )

    raw_dir = out_dir / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    # One render pass per framerate (separate CS2 launch per pass)
    configs = build_csdm_configs(job, demo_path, raw_dir)
    config_paths: list[Path] = []
    for i, config in enumerate(configs, start=1):
        config_path = work_dir / f"{run_id}.csdm-config-{i}.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        config_paths.append(config_path)
        log(
            f"pass {i}/{len(configs)}: {len(config['sequences'])} sequences "
            f"@ {config['framerate']} fps — launching CS2 + rendering, "
            f"DON'T use the PC now ...",
            log_dir,
        )
        code, output = run_csdm(
            csdm, ["video", "--config-file", str(config_path)], log_dir
        )
        if code != 0:
            hint = ""
            if "hlae" in output.lower():
                hint = (
                    " (HLAE-related? Game updates break HLAE — update it in "
                    "CS Demo Manager → Settings → Video and try again.)"
                )
            raise RuntimeError(
                f"csdm video failed in pass {i}/{len(configs)} "
                f"(exit {code}).{hint}"
            )

    # Alternate-POV sequences map to <clip_id>_alt.mp4
    all_clips = list(job["clips"])
    for clip in job["clips"]:
        if clip.get("alt_pov_steamid"):
            all_clips.append(
                {
                    "clip_id": clip["clip_id"] + "_alt",
                    "number": clip["number"] + ALT_SEQ_OFFSET,
                }
            )
    mapping, warnings = map_outputs(raw_dir, all_clips)
    result["warnings"] = warnings
    for clip in all_clips:
        src = mapping.get(clip["clip_id"])
        if src is None:
            result["clips"][clip["clip_id"]] = "missing"
            log(f"WARNING: no output for {clip['clip_id']}", log_dir)
            continue
        # .mp4 name regardless of source container — ffmpeg reads content, not names
        shutil.move(src, out_dir / f"{clip['clip_id']}.mp4")
        result["clips"][clip["clip_id"]] = "ok"
    if len(mapping) == len(all_clips):
        shutil.rmtree(raw_dir, ignore_errors=True)
    else:
        # NEVER delete evidence when something is missing — list what's there
        leftovers = [str(p.relative_to(raw_dir)) for p in raw_dir.rglob("*") if p.is_file()]
        result["raw_leftovers"] = leftovers[:50]
        log(f"keeping _raw for troubleshooting ({len(leftovers)} files)", log_dir)

    done = [v for v in result["clips"].values() if v == "ok"]
    # csdm can return exit 0 even when playback failed (e.g.
    # "demo file incompatible") — the number of actual output files is the truth.
    if len(done) == len(all_clips):
        result["status"] = "ok"
    elif done:
        result["status"] = "partial"
    else:
        result["status"] = "failed"
        result["error"] = (
            "csdm ran, but no clips were produced. Typical causes: "
            "the demo is from an older CS2 version (playback refused), or "
            "HLAE broke after a game update."
        )
    log(f"job {run_id}: {result['status']} ({len(done)}/{len(all_clips)} clips)", log_dir)
    for cp in config_paths:
        cp.unlink(missing_ok=True)
    return result


def process_job(job_path: Path, job_dir: Path, csdm: Path) -> bool:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    run_id = job["run_id"]
    log(f"=== job {run_id}: {len(job['clips'])} clips ===", job_dir)

    working = job_dir / "working" / job_path.name
    shutil.move(job_path, working)

    result: dict = {"run_id": run_id, "status": "failed", "warnings": [], "clips": {}}
    result_path = job_dir / "outbox" / f"{run_id}.result.json"

    try:
        result = render_job(
            job,
            demo_path=job_dir / "demos" / job["demo_filename"],
            out_dir=job_dir / "outbox" / run_id,
            csdm=csdm,
            log_dir=job_dir,
            work_dir=job_dir / "working",
        )
        working.unlink(missing_ok=True)
        return True

    except Exception as exc:  # noqa: BLE001 — everything goes into the result file
        result["error"] = str(exc)
        result["versions"] = detect_versions(csdm)
        log(f"ERROR in job {run_id}: {exc}", job_dir)
        return False
    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--job-dir", required=True, help="Job queue folder (shared or local)")
    ap.add_argument("--csdm", default=str(DEFAULT_CSDM), help="Path to csdm.cmd/csdm.exe")
    ap.add_argument("--once", action="store_true", help="Process one job and exit")
    args = ap.parse_args()

    job_dir = Path(args.job_dir)
    csdm = Path(args.csdm)
    if not csdm.exists():
        print(f"ERROR: cannot find csdm at {csdm} — use --csdm <path>")
        return 1
    for sub in ("demos", "inbox", "working", "outbox"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    # Resume jobs left in working/ after a crash
    for stuck in (job_dir / "working").glob("*.json"):
        # csdm config files (one per render pass) are not jobs
        if ".csdm-config" not in stuck.name:
            log(f"moving interrupted job back to inbox: {stuck.name}", job_dir)
            shutil.move(stuck, job_dir / "inbox" / stuck.name)

    log(f"worker started — waiting for jobs in {job_dir / 'inbox'}", job_dir)
    consecutive_failures = 0
    while True:
        jobs = sorted((job_dir / "inbox").glob("*.json"))
        if jobs:
            ok = process_job(jobs[0], job_dir, csdm)
            consecutive_failures = 0 if ok else consecutive_failures + 1
            if consecutive_failures >= 2:
                log(
                    "STOPPING: 2 job failures in a row. Check whether a game update "
                    "broke HLAE (update it in CS Demo Manager → Settings → Video; "
                    "updating CS Demo Manager itself doesn't), see worker.log.",
                    job_dir,
                )
                return 1
            if args.once:
                return 0 if ok else 1
        else:
            if args.once:
                log("no jobs in the queue.", job_dir)
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
