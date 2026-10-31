"""`python main.py doctor` and `python main.py setup`.

doctor: checks everything Fragsmith needs for the configured setup (Python
packages, ffmpeg, config.toml, Director, rendering) and says what's missing.
Read-only, except a harmless `echo` over SSH in ssh mode.

setup: asks a few questions (SteamID, music folder, one or two PCs) and
writes config.toml from config.example.toml, keeping all its comments.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Callable

PLACEHOLDER_STEAMID = "76561190000000000"
STEAMID_RE = re.compile(r"^7656119\d{10}$")


class Report:
    def __init__(self) -> None:
        self.fails = 0
        self.warns = 0

    def ok(self, msg: str) -> None:
        print(f"  OK    {msg}")

    def warn(self, msg: str, hint: str = "") -> None:
        self.warns += 1
        print(f"  WARN  {msg}")
        if hint:
            print(f"        → {hint}")

    def fail(self, msg: str, hint: str = "") -> None:
        self.fails += 1
        print(f"  FAIL  {msg}")
        if hint:
            print(f"        → {hint}")


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


def _check_python(r: Report) -> None:
    v = sys.version_info
    if v >= (3, 11):
        r.ok(f"Python {v.major}.{v.minor}.{v.micro}")
    else:
        r.fail(f"Python {v.major}.{v.minor} is too old", "Fragsmith needs Python 3.11+.")

    missing = [
        name for name in ("demoparser2", "pydantic", "librosa")
        if importlib.util.find_spec(name) is None
    ]
    if missing:
        r.fail(
            f"Python packages missing: {', '.join(missing)}",
            "pip install -r requirements.txt",
        )
    else:
        r.ok("Python packages: demoparser2, pydantic, librosa")


def _check_ffmpeg(r: Report) -> None:
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        if not path:
            r.fail(
                f"{tool} not found on PATH",
                "Install ffmpeg (https://ffmpeg.org/download.html) and make sure "
                f"'{tool}' runs in a new terminal.",
            )
            continue
        proc = subprocess.run(
            [path, "-version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        first = (proc.stdout.splitlines() or ["?"])[0]
        r.ok(f"{tool}: {' '.join(first.split()[:3])}")


def _check_config(r: Report, project_dir: Path) -> dict | None:
    path = project_dir / "config.toml"
    if not path.exists():
        r.fail("config.toml is missing", "python main.py setup (or copy config.example.toml)")
        return None
    try:
        with open(path, "rb") as fh:
            cfg = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        r.fail(f"config.toml has a syntax error: {exc}")
        return None
    r.ok("config.toml found")

    players = [str(p) for p in cfg.get("players", {}).get("known_players", [])]
    bad = [p for p in players if not STEAMID_RE.match(p)]
    if not players or players == [PLACEHOLDER_STEAMID]:
        r.fail(
            "[players].known_players still has the example SteamID",
            "Put your own SteamID64 there (find it at https://steamid.io).",
        )
    elif bad:
        r.fail(
            f"Not valid SteamID64s: {', '.join(bad)}",
            "A SteamID64 is 17 digits and starts with 7656119.",
        )
    else:
        r.ok(f"{len(players)} Steam account(s) in known_players")

    from lib.director import list_music_files

    music_dir = str(cfg.get("music", {}).get("music_dir", ""))
    tracks = list_music_files(music_dir)
    if not Path(music_dir).is_dir():
        r.fail(f"Music folder not found: '{music_dir}'", "Set [music].music_dir.")
    elif not tracks:
        r.fail(f"No music files in {music_dir}", "Add mp3/wav/flac/ogg/m4a files.")
    else:
        r.ok(f"{len(tracks)} music track(s) in {music_dir}")

    lut = str(cfg.get("effects", {}).get("lut", "") or "")
    if lut:
        lut_path = Path(lut) if Path(lut).is_absolute() else project_dir / lut
        if not lut_path.is_file():
            r.warn(
                f"LUT file not found: {lut} (grading will run without it)",
                "python tools/make_lut.py, or set [effects].lut = \"\".",
            )
    return cfg


def _check_director(r: Report, cfg: dict) -> None:
    mode = str(cfg.get("director", {}).get("mode", "auto")).lower()
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    has_pkg = importlib.util.find_spec("anthropic") is not None
    if mode not in ("auto", "rules", "claude"):
        r.fail(f"Unknown [director].mode '{mode}'", "Use auto, rules or claude.")
    elif mode == "rules":
        r.ok("Director: built-in rules")
    elif mode == "claude" and not has_key:
        r.fail(
            "Director mode is 'claude' but ANTHROPIC_API_KEY is missing",
            "Put ANTHROPIC_API_KEY=... in a .env file, or set mode = \"auto\".",
        )
    elif has_key and not has_pkg:
        (r.fail if mode == "claude" else r.warn)(
            "ANTHROPIC_API_KEY is set but the anthropic package is missing",
            "pip install anthropic",
        )
    elif has_key:
        r.ok("Director: Claude (API key found)")
    else:
        r.ok("Director: built-in rules (no API key; that's fine)")


def _check_render(r: Report, cfg: dict) -> None:
    from lib.render_clips import csdm_path, render_mode

    render_cfg = cfg.get("render", {})
    try:
        mode = render_mode(render_cfg)
    except ValueError as exc:
        r.fail(str(exc))
        return
    label = {"local": "one PC", "ssh": "render PC over SSH",
             "folder": "render PC via shared folder/USB"}[mode]
    r.ok(f"Render mode: {mode} ({label})")

    if mode == "local":
        csdm = csdm_path(render_cfg)
        if csdm.exists():
            r.ok(f"CS Demo Manager: {csdm}")
        else:
            r.fail(
                f"CS Demo Manager not found at {csdm}",
                "Install it from https://cs-demo-manager.com, open it once and "
                "finish the setup wizard. Or set [render].csdm_path.",
            )
        import worker

        if worker.csdm_app_running():
            r.warn(
                "The CS Demo Manager app is open",
                "Close it before rendering (check the system tray too). While "
                "it runs, CS2 connects to the app and rendering hangs.",
            )
        cs2 = find_cs2()
        if cs2:
            r.ok(f"CS2: {cs2}")
        else:
            r.warn(
                "CS2 not found in your Steam libraries",
                "Rendering needs CS2 installed on this PC. Ignore this if it "
                "is installed somewhere unusual.",
            )
        return

    if mode == "ssh":
        host = render_cfg.get("ssh_host", "")
        if not host:
            r.fail("[render].ssh_host is empty", "e.g. ssh_host = \"user@192.168.1.50\"")
            return
        if not shutil.which("ssh") or not shutil.which("scp"):
            r.fail("ssh/scp not found on PATH",
                   "Enable Windows' built-in OpenSSH Client (Optional features).")
            return
        key = render_cfg.get("ssh_key", "")
        if key and not Path(key).exists():
            r.fail(f"SSH key not found: {key}")
            return
        from lib.render_clips import _ssh_opts

        remote = str(render_cfg.get("remote_job_dir", "C:/fragsmith-jobs")).replace("/", "\\")
        try:
            proc = subprocess.run(
                ["ssh", *_ssh_opts(render_cfg), host,
                 f'if exist "{remote}\\inbox" (echo YES) else (echo NO)'],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30,
            )
        except subprocess.TimeoutExpired:
            r.fail(f"SSH to {host} timed out", "Is the render PC on and reachable?")
            return
        if proc.returncode != 0:
            err = proc.stderr.strip()[-200:]
            unreachable = any(s in err.lower() for s in ("timed out", "refused", "no route"))
            r.fail(
                f"SSH to {host} failed: {err}",
                "Is the render PC on and on the same network?" if unreachable
                else "See docs/two-pc-setup.md (OpenSSH server + key login).",
            )
        elif "YES" in proc.stdout:
            r.ok(f"SSH to {host} works, job folder found")
        else:
            r.warn(
                f"SSH to {host} works, but {remote}\\inbox doesn't exist yet",
                "Start the worker once on the render PC: "
                f"python worker.py --job-dir {remote}",
            )
        return

    job_dir = str(render_cfg.get("job_dir", "") or "")
    if not job_dir:
        r.ok("No job_dir: 'render' writes the job for manual USB transfer")
    elif not Path(job_dir).is_dir():
        r.fail(f"Job folder not reachable: {job_dir}",
               "Is the render PC on and the folder shared?")
    elif not (Path(job_dir) / "inbox").is_dir():
        r.warn(f"{job_dir} has no inbox/ yet",
               "Start the worker once on the render PC.")
    else:
        r.ok(f"Job folder reachable: {job_dir}")


def _check_disk(r: Report, project_dir: Path) -> None:
    free_gb = shutil.disk_usage(project_dir).free / 1e9
    if free_gb < 5:
        r.warn(f"Only {free_gb:.1f} GB free", "Raw clips take ~40 MB per minute.")
    else:
        r.ok(f"{free_gb:.0f} GB free disk space")


def run_doctor(project_dir: Path) -> int:
    print("Fragsmith doctor")
    r = Report()
    _check_python(r)
    _check_ffmpeg(r)
    cfg = _check_config(r, project_dir)
    if cfg is not None:
        _check_director(r, cfg)
        _check_render(r, cfg)
    _check_disk(r, project_dir)
    print()
    if r.fails:
        print(f"{r.fails} problem(s), {r.warns} warning(s). Fix the FAIL lines first.")
        return 1
    if r.warns:
        print(f"Ready, with {r.warns} warning(s).")
    else:
        print("Everything looks ready. Try: python main.py run <demo.dem>")
    return 0


# --- setup ---------------------------------------------------------------


def _toml_str(value: str) -> str:
    # literal strings keep Windows backslashes readable
    if "'" not in value and "\n" not in value:
        return f"'{value}'"
    return json.dumps(value)


def _set_value(text: str, key: str, value: str) -> str:
    """Replace the value of `key = "..."` (first match), keeping any comment."""
    pattern = rf'^({re.escape(key)}\s*=\s*)("[^"\n]*"|\'[^\'\n]*\')'
    new, n = re.subn(pattern, lambda m: m.group(1) + value, text, count=1, flags=re.M)
    if n != 1:
        raise RuntimeError(f"config.example.toml has no '{key} = \"...\"' line.")
    return new


def _ask(prompt: str, ask: Callable[[str], str], default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = ask(f"{prompt}{suffix}: ").strip().strip('"')
    return answer or default


def _choose(prompt: str, options: dict[str, str], ask: Callable[[str], str]) -> str:
    print(prompt)
    for k, label in options.items():
        print(f"  {k}) {label}")
    while True:
        answer = _ask("Choose", ask, "1")
        if answer in options:
            return answer
        print(f"  Please answer {' or '.join(options)}.")


def build_config(example: str, answers: dict) -> str:
    """config.example.toml text + setup answers → config.toml text."""
    ids = ",\n".join(f'    "{sid}"' for sid in answers["steamids"])
    block = re.compile(r"^known_players = \[.*?^\]", re.M | re.S)
    text, n = block.subn(lambda m: f"known_players = [\n{ids},\n]", example, count=1)
    if n != 1:
        raise RuntimeError("config.example.toml has no known_players list.")
    text = _set_value(text, "music_dir", _toml_str(answers["music_dir"]))
    # [director] has a mode key too: only search from [render] onward
    # ([render] comes first, so its keys are the first matches there)
    head, sep, render = text.partition("\n[render]\n")
    if not sep:
        raise RuntimeError("config.example.toml has no [render] section.")
    for key in ("mode", "csdm_path", "ssh_host", "ssh_key", "remote_job_dir", "job_dir"):
        if key in answers:
            value = f'"{answers[key]}"' if key == "mode" else _toml_str(answers[key])
            render = _set_value(render, key, value)
    text = head + sep + render
    tomllib.loads(text)  # never write a config that doesn't parse
    return text


def run_setup(project_dir: Path, ask: Callable[[str], str] = input) -> int:
    target = project_dir / "config.toml"
    example = project_dir / "config.example.toml"
    print("Fragsmith setup — writes config.toml. Press Enter to accept [defaults].\n")
    if target.exists():
        if _ask("config.toml already exists. Overwrite it? (y/N)", ask, "n").lower() != "y":
            print("Nothing changed.")
            return 0

    while True:
        raw = _ask(
            "Your SteamID64 (17 digits, find it at https://steamid.io; "
            "several accounts separated by commas)", ask,
        )
        steamids = [s.strip() for s in raw.split(",") if s.strip()]
        bad = [s for s in steamids if not STEAMID_RE.match(s)]
        if steamids and not bad:
            break
        print("  A SteamID64 is 17 digits and starts with 7656119. Try again.")

    from lib.director import list_music_files

    music_dir = _ask("Folder with your music (mp3/wav/flac)", ask)
    if not list_music_files(music_dir):
        print("  (No music files there yet; add some before your first run.)")

    answers: dict = {"steamids": steamids, "music_dir": music_dir}
    pcs = _choose(
        "\nWhich PC renders the clips? (rendering launches CS2 and takes over "
        "that PC for a while)",
        {"1": "This PC (CS2 + CS Demo Manager are installed here)",
         "2": "A separate render PC"},
        ask,
    )
    if pcs == "1":
        answers["mode"] = "local"
        from lib.render_clips import csdm_path

        default = csdm_path({})
        if not default.exists():
            custom = _ask(
                f"CS Demo Manager wasn't found at {default}. Path to csdm.cmd "
                "(Enter to skip and install it later)", ask,
            )
            answers["csdm_path"] = custom
    else:
        how = _choose(
            "\nHow do jobs get to the render PC?",
            {"1": "SSH (recommended: fully automatic)",
             "2": "Shared network folder",
             "3": "USB stick (you copy files by hand)"},
            ask,
        )
        if how == "1":
            answers["mode"] = "ssh"
            answers["ssh_host"] = _ask("Render PC as user@address", ask)
            answers["ssh_key"] = _ask("SSH key file (Enter for your default key)", ask)
            answers["remote_job_dir"] = _ask(
                "Job folder on the render PC", ask, "C:/fragsmith-jobs"
            )
        elif how == "2":
            answers["mode"] = "folder"
            answers["job_dir"] = _ask(r"Shared folder path (e.g. \\RENDER-PC\fragsmith-jobs)", ask)
        else:
            answers["mode"] = "folder"
            answers["job_dir"] = ""

    text = build_config(example.read_text(encoding="utf-8"), answers)
    target.write_text(text, encoding="utf-8")
    print(f"\nWrote {target}. All other settings (effects, editing) keep their defaults.")
    if answers["mode"] != "local":
        print("On the render PC: see docs/two-pc-setup.md, then run worker.py there.")
    print()
    return run_doctor(project_dir)
