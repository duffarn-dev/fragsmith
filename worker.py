"""Fragsmith render-worker — kjøres på RENDER-PC-en (med CS2 + CS Demo Manager).

Kun standardbibliotek: trenger bare Python 3.10+ installert.

    python worker.py --job-dir <delt mappe eller lokal kø> [--csdm <sti til csdm.exe>] [--once]

Plukker jobber fra <job_dir>/inbox/, kjører per jobb:
  1. csdm analyze <demo>       (hopper over demoer som alt er i databasen)
  2. csdm video --config-file  (HLAE + FFmpeg; ALLE klipp i én CS2-oppstart)
  3. Døper om outputfilene til <clip_id>.mp4 i <job_dir>/outbox/<run_id>/

VIKTIG:
- PC-en må være innlogget med skrivebordssesjon — CS2 starter og er i bruk
  under rendering. Ikke rør maskinen mens det pågår.
- Rendering skjer aldri parallelt: én jobb, én sekvens om gangen.
- To jobbfeil på rad → workeren stopper seg selv (typisk tegn på at en
  spilloppdatering har brukket HLAE; oppdater CS Demo Manager og prøv igjen).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# CLI-et er csdm.cmd (ikke .exe) i installasjonsmappa — verifisert på render-PC
DEFAULT_CSDM = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Programs" / "cs-demo-manager" / "csdm.cmd"
)
POLL_SECONDS = 10


def log(msg: str, job_dir: Path | None = None) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    if job_dir is not None:
        with open(job_dir / "worker.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def run_csdm(csdm: Path, args: list[str], job_dir: Path) -> tuple[int, str]:
    cmd = [str(csdm), *args]
    log(f"kjører: {subprocess.list2cmdline(cmd)}", job_dir)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    for line in output.splitlines():
        if line.strip():
            log(f"  csdm: {line.strip()}", job_dir)
    return proc.returncode, output


ALT_SEQ_OFFSET = 100  # alt-POV-sekvenser (P17) nummereres base + 100


def build_csdm_configs(job: dict, demo_path: Path, raw_dir: Path) -> list[dict]:
    """Én csdm-config per framerate i jobben (P20). csdm har GLOBALT
    framerate — klipp med eget "framerate"-felt (schema 1.3, slow-mo-klipp
    i høy fps) samles derfor i et eget pass med egen CS2-oppstart.
    Eldre jobber (uten feltet) gir nøyaktig én config som før.
    Sekvensnummer er unike på tvers av passene, så output-mappingen
    (sequence-N-...) er upåvirket."""
    video = job["video"]
    base_fps = int(video["framerate"])
    by_fps: dict[int, list[dict]] = {}
    for clip in job["clips"]:
        fps = int(clip.get("framerate", base_fps))
        seq = {
            "number": clip["number"],
            "startTick": clip["start_tick"],
            "endTick": clip["end_tick"],
            # P2: per-klipp X-ray fra dev-siden (ace/clutch/AWP);
            # eldre jobber (schema 1.0) mangler feltet → av
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
        # P17: ekstra sekvens med samme tick-range fra OFFERETS POV
        # (instant replay, P19) — arver klippets framerate (replayen
        # bremser nettopp denne vinkelen). Eldre jobber: ingen ekstra.
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
            # P2: riktig POV-kropp/armer. Kan i sjeldne tilfeller skjule
            # crosshair — verifiser per CS2-versjon (learnings_runtime.md)
            "trueView": bool(job.get("true_view", False)),
            # Eksplisitt — csdm sin DEFAULT container er AVI (+ mp3-lyd,
            # crf 23); vi vil ha forutsigbar mp4/aac
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
        for fps, seqs in sorted(by_fps.items())  # base-fps-passet først
    ]


VIDEO_EXTS = {".mp4", ".avi", ".mkv"}


def map_outputs(raw_dir: Path, clips: list[dict]) -> tuple[dict[str, Path], list[str]]:
    """Map produserte videofiler til clip_id via csdm sitt navnemønster:
    sequence-<N>-tick-<start>-to-<slutt>.<container>  (get-sequence-output-file-path.ts)
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
            "Filnavn fulgte ikke sequence-N-mønsteret — mappet i sortert "
            "rekkefølge i stedet (verifiser klippene!)."
        )
        mapping = {
            clip["clip_id"]: f
            for clip, f in zip(sorted(clips, key=lambda c: c["number"]), produced)
        }
    return mapping, warnings


def process_job(job_path: Path, job_dir: Path, csdm: Path) -> bool:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    run_id = job["run_id"]
    log(f"=== jobb {run_id}: {len(job['clips'])} klipp ===", job_dir)

    working = job_dir / "working" / job_path.name
    shutil.move(job_path, working)

    result: dict = {"run_id": run_id, "status": "feil", "warnings": [], "clips": {}}
    result_path = job_dir / "outbox" / f"{run_id}.result.json"

    try:
        demo_path = job_dir / "demos" / job["demo_filename"]
        if not demo_path.exists():
            raise RuntimeError(f"Demofil mangler: {demo_path}")

        code, _ = run_csdm(csdm, ["analyze", str(demo_path)], job_dir)
        if code != 0:
            raise RuntimeError(f"csdm analyze feilet (exit {code}).")

        raw_dir = job_dir / "outbox" / run_id / "_raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        # P20: ett render-pass per framerate (egen CS2-oppstart per pass)
        configs = build_csdm_configs(job, demo_path, raw_dir)
        config_paths: list[Path] = []
        for i, config in enumerate(configs, start=1):
            config_path = (
                job_dir / "working" / f"{run_id}.csdm-config-{i}.json"
            )
            config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
            config_paths.append(config_path)
            log(
                f"pass {i}/{len(configs)}: {len(config['sequences'])} sekvenser "
                f"@ {config['framerate']} fps — starter CS2 + rendering, "
                f"IKKE bruk PC-en nå ...",
                job_dir,
            )
            code, output = run_csdm(
                csdm, ["video", "--config-file", str(config_path)], job_dir
            )
            if code != 0:
                hint = ""
                if "hlae" in output.lower():
                    hint = (
                        " (HLAE-relatert? Spilloppdateringer brekker HLAE — "
                        "oppdater CS Demo Manager og prøv igjen.)"
                    )
                raise RuntimeError(
                    f"csdm video feilet i pass {i}/{len(configs)} "
                    f"(exit {code}).{hint}"
                )

        # P17: alt-POV-sekvenser mappes til <clip_id>_alt.mp4
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
        out_dir = job_dir / "outbox" / run_id
        for clip in all_clips:
            src = mapping.get(clip["clip_id"])
            if src is None:
                result["clips"][clip["clip_id"]] = "mangler"
                log(f"ADVARSEL: intet output for {clip['clip_id']}", job_dir)
                continue
            # .mp4-navn uansett kilde-container — ffmpeg leser innhold, ikke navn
            shutil.move(src, out_dir / f"{clip['clip_id']}.mp4")
            result["clips"][clip["clip_id"]] = "ok"
        if len(mapping) == len(all_clips):
            shutil.rmtree(raw_dir, ignore_errors=True)
        else:
            # ALDRI slett bevis når noe mangler — list det som ligger der
            leftovers = [str(p.relative_to(raw_dir)) for p in raw_dir.rglob("*") if p.is_file()]
            result["raw_leftovers"] = leftovers[:50]
            log(f"beholder _raw for feilsøking ({len(leftovers)} filer)", job_dir)

        done = [v for v in result["clips"].values() if v == "ok"]
        # csdm kan returnere exit 0 selv om avspillingen feilet (f.eks.
        # "demo file incompatible") — antall faktiske output-filer er fasiten.
        if len(done) == len(all_clips):
            result["status"] = "ok"
        elif done:
            result["status"] = "delvis"
        else:
            result["status"] = "feil"
            result["error"] = (
                "csdm kjørte, men ingen klipp ble produsert. Typiske årsaker: "
                "demoen er fra en eldre CS2-versjon (avspilling nektes), eller "
                "HLAE brakk etter en spilloppdatering."
            )
        log(f"jobb {run_id}: {result['status']} ({len(done)}/{len(all_clips)} klipp)", job_dir)
        working.unlink(missing_ok=True)
        for cp in config_paths:
            cp.unlink(missing_ok=True)
        return True

    except Exception as exc:  # noqa: BLE001 — alt skal i result-fila
        result["error"] = str(exc)
        log(f"FEIL i jobb {run_id}: {exc}", job_dir)
        return False
    finally:
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--job-dir", required=True, help="Jobbkø-mappe (delt/lokal)")
    ap.add_argument("--csdm", default=str(DEFAULT_CSDM), help="Sti til csdm.exe")
    ap.add_argument("--once", action="store_true", help="Prosesser én jobb og avslutt")
    args = ap.parse_args()

    job_dir = Path(args.job_dir)
    csdm = Path(args.csdm)
    if not csdm.exists():
        print(f"FEIL: finner ikke csdm.exe på {csdm} — bruk --csdm <sti>")
        return 1
    for sub in ("demos", "inbox", "working", "outbox"):
        (job_dir / sub).mkdir(parents=True, exist_ok=True)

    # Gjenoppta jobber som ble stående i working/ etter krasj
    for stuck in (job_dir / "working").glob("*.json"):
        # csdm-config-filer (én per render-pass, P20) er ikke jobber
        if ".csdm-config" not in stuck.name:
            log(f"flytter avbrutt jobb tilbake til inbox: {stuck.name}", job_dir)
            shutil.move(stuck, job_dir / "inbox" / stuck.name)

    log(f"worker startet — venter på jobber i {job_dir / 'inbox'}", job_dir)
    consecutive_failures = 0
    while True:
        jobs = sorted((job_dir / "inbox").glob("*.json"))
        if jobs:
            ok = process_job(jobs[0], job_dir, csdm)
            consecutive_failures = 0 if ok else consecutive_failures + 1
            if consecutive_failures >= 2:
                log(
                    "STOPPER: 2 jobbfeil på rad. Sjekk om en spilloppdatering "
                    "har brukket HLAE (oppdater CS Demo Manager), se worker.log.",
                    job_dir,
                )
                return 1
            if args.once:
                return 0 if ok else 1
        else:
            if args.once:
                log("ingen jobber i køen.", job_dir)
                return 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
