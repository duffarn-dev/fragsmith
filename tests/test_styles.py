"""Tests for the style presets (lib/styles.py) and the style in the edit plan.

Covers: picking the style (--style, config, default), laying a preset over the
config without touching anything else, preset keys that really exist in
config.example.toml, what the presets change in the render (replay, high fps),
old plans without a style, and the style line in report.md.

Run: python tests/test_styles.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.edit_engine import pick_replay_clip, wants_high_fps  # noqa: E402
from lib.models import EditPlan, Highlight, KillMark  # noqa: E402
from lib.rules_director import run_rules_director  # noqa: E402
from lib.styles import STYLES, apply_style, check_style, resolve_style  # noqa: E402

FAILS: list[str] = []

EXAMPLE = tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_resolve() -> None:
    print("picking the style")
    check("default is custom", resolve_style({}) == "custom")
    check("config preset", resolve_style({"style": {"preset": "hype"}}) == "hype")
    check("--style wins over the config",
          resolve_style({"style": {"preset": "hype"}}, "clean") == "clean")
    check("case and spaces don't matter", check_style(" Cinematic ") == "cinematic")
    try:
        check_style("loud")
        check("unknown style raises", False)
    except ValueError as exc:
        check("unknown style raises and names the valid ones",
              all(s in str(exc) for s in STYLES), str(exc))
    check("the example config ships with custom", EXAMPLE["style"]["preset"] == "custom")


def test_apply() -> None:
    print("laying a preset over the config")
    cfg = {
        "effects": {"enabled": True, "lut": "my.cube", "kill_zoom": False, "eased_ramps": False},
        "editing": {"game_audio_volume": 0.4, "sfx": False},
        "video": {"framerate": 60},
    }
    before = repr(cfg)
    check("custom changes nothing", apply_style(cfg, "custom") == cfg)
    hype = apply_style(cfg, "hype")
    check("the input is never changed", repr(cfg) == before)
    check("hype turns its effects on", hype["effects"]["kill_zoom"] is True
          and hype["editing"]["sfx"] is True)
    check("keys a preset doesn't name keep the user's value",
          hype["effects"]["lut"] == "my.cube" and hype["effects"]["eased_ramps"] is False
          and hype["editing"]["game_audio_volume"] == 0.4 and hype["video"] == {"framerate": 60})
    off = apply_style({"effects": {"enabled": False}}, "hype")
    check("enabled = false stays the master switch", off["effects"]["enabled"] is False)
    check("a missing section is created", apply_style({}, "clean")["effects"]["kill_zoom"] is False)

    for name, preset in STYLES.items():
        for section, values in preset.items():
            unknown = set(values) - set(EXAMPLE.get(section, {}))
            check(f"{name}: every [{section}] key exists in config.example.toml",
                  not unknown, str(sorted(unknown)) if unknown else "")
        check(f"{name}: leaves enabled, lut and the quality switches alone",
              not {"enabled", "lut", "eased_ramps", "smooth_slowmo"} & set(preset.get("effects", {})))
    looks = {name: tuple(sorted(p.get("effects", {}).items())) for name, p in STYLES.items()}
    check("the three presets are all different", len(set(looks.values())) == len(looks))


def three_kill_clip() -> list[Highlight]:
    kills = [KillMark(tick=64 * (2 + 2 * i), offset_s=2.0 + 2 * i, weapon="ak47", headshot=True,
                      victim_name=f"v{i}", victim_steamid=f"7656119000000010{i}") for i in range(3)]
    best = Highlight(clip_id="r19_multikill_00", start_tick=0, end_tick=640,
                     player_steamid="76561190000000001", kill_count=3, kind="multikill",
                     hype_score=6.0, round=19, duration_seconds=10.0, headshots=3, kills=kills)
    opener = Highlight(clip_id="r04_multikill_01", start_tick=0, end_tick=640,
                       player_steamid="76561190000000001", kill_count=2, kind="multikill",
                       hype_score=3.0, round=4, duration_seconds=10.0, headshots=2, kills=kills[:2])
    return [opener, best]


def test_render_side() -> None:
    print("what the presets change in the render")
    chosen = three_kill_clip()
    by_id = {c.clip_id: c for c in chosen}
    plan = run_rules_director(chosen, {"demo_path": "x.dem"}, ["click.wav"], "pov")
    base = {k: dict(v) if isinstance(v, dict) else v for k, v in EXAMPLE.items()}

    def summary(style: str) -> tuple[bool, set[str]]:
        cfg = apply_style(base, style)
        fx = cfg["effects"]
        high = {d.clip_id for d in plan.decisions if wants_high_fps(d, by_id[d.clip_id], fx)}
        return pick_replay_clip(plan, by_id, cfg) is not None, high

    clean_replay, clean_high = summary("clean")
    hype_replay, hype_high = summary("hype")
    cine_replay, _ = summary("cinematic")
    check("clean: no instant replay", not clean_replay)
    check("hype and cinematic: the 3k climax gets a replay", hype_replay and cine_replay)
    check("hype: headshot slow-mo makes more clips high fps than clean",
          len(hype_high) > len(clean_high), f"{hype_high} vs {clean_high}")


def test_plan() -> None:
    print("the style in the edit plan")
    old = ('{"demo_path": "x.dem", "music_track": "a.mp3", "intro_text": "", "outro_text": "",'
           ' "decisions": [], "dropped_clip_ids": [], "schema_version": "1.1"}')
    check("a plan from before styles reads as custom",
          EditPlan.model_validate_json(old).style == "custom")
    plan = EditPlan.model_validate_json(old)
    plan.style = "cinematic"
    again = EditPlan.model_validate_json(plan.model_dump_json())
    check("the style survives saving and loading", again.style == "cinematic")


def test_report(tmp: Path) -> None:
    print("report.md names the style")
    from lib.assemble import write_report

    film = tmp / "final.mp4"
    subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc2=s=320x180:r=30:d=1", "-pix_fmt", "yuv420p", str(film)],
                   check=True)
    plan = EditPlan(demo_path="x.dem", music_track="a.mp3", intro_text="", outro_text="",
                    decisions=[], dropped_clip_ids=[], style="hype")
    report = write_report(plan, {}, [], [], {"bpm": 120}, film, tmp)
    check("**Style:** hype", "**Style:** hype" in report.read_text(encoding="utf-8"))


if __name__ == "__main__":
    test_resolve()
    test_apply()
    test_render_side()
    test_plan()
    with tempfile.TemporaryDirectory() as t:
        test_report(Path(t))
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All style tests OK")
