"""Style presets: one word for a tuned set of effects.

A preset overrides only the keys it names in [effects]/[editing]; everything
else keeps the user's value. "custom" overrides nothing, so the config is used
exactly as written.

Left alone on purpose:
- [effects].enabled stays the master switch (false still turns everything off)
- lut, eased_ramps, smooth_slowmo: the user's own file and quality/speed trade-offs

The style is chosen at `run` and stored in the edit plan, because some effects
decide what gets rendered (slow-mo at high fps, the instant replay's extra
pass). `render` and `finish` apply the plan's style, never the config's.
"""
from __future__ import annotations

DEFAULT_STYLE = "custom"

STYLES: dict[str, dict[str, dict]] = {
    "custom": {},
    # Grade and beat cuts. No zooms, flashes, counters or sound effects.
    "clean": {
        "effects": {
            "intensity": 0.8,
            "grade": True,
            "vignette": True,
            "beat_zoom": False,
            "flash_transitions": False,
            "wobble": False,
            "kill_zoom": False,
            "kill_tilt": False,
            "intro_hook": False,
            "slowmo_punch": False,
            "letterbox_on_slowmo": False,
            "grain": 0.0,
            "freeze_on_finish": False,
            "headshot_punch": False,
            "headshot_micro_slowmo": False,
            "kill_counter": False,
            "lowerthird": True,
            "animated_overlays": True,
            "impact_split": False,
            "instant_replay": False,
        },
        "editing": {"sfx": False},
    },
    # Everything on and stronger: the classic fragmovie.
    "hype": {
        "effects": {
            "intensity": 1.5,
            "grade": True,
            "vignette": True,
            "beat_zoom": True,
            "beat_zoom_strength": 0.8,
            "flash_transitions": True,
            "wobble": True,
            "kill_zoom": True,
            "kill_zoom_factor": 1.8,
            "kill_tilt": True,
            "intro_hook": True,
            "slowmo_punch": True,
            "letterbox_on_slowmo": False,
            "grain": 0.1,
            "freeze_on_finish": True,
            "freeze_seconds": 0.4,
            "headshot_punch": True,
            "headshot_punch_strength": 1.2,
            "headshot_micro_slowmo": True,
            "kill_counter": True,
            "lowerthird": True,
            "animated_overlays": True,
            "impact_split": True,
            "instant_replay": True,
        },
        "editing": {"sfx": True, "sfx_volume": 0.45},
    },
    # Film look: letterbox, grain, slow pushes, long freezes, the replay.
    # Nothing that shakes or flashes.
    "cinematic": {
        "effects": {
            "intensity": 1.0,
            "grade": True,
            "vignette": True,
            "beat_zoom": False,
            "flash_transitions": False,
            "wobble": False,
            "kill_zoom": True,
            "kill_zoom_factor": 1.4,
            "kill_tilt": False,
            "intro_hook": True,
            "slowmo_punch": True,
            "letterbox_on_slowmo": True,
            # h264 at crf 18 smooths fine noise away: 0.25 was invisible next
            # to "clean" in a 1:1 crop, 0.5 shows (mostly in motion)
            "grain": 0.5,
            "freeze_on_finish": True,
            "freeze_seconds": 0.7,
            "headshot_punch": False,
            "headshot_micro_slowmo": False,
            "kill_counter": False,
            "lowerthird": True,
            "animated_overlays": True,
            "impact_split": False,
            "instant_replay": True,
        },
        "editing": {"sfx": True, "sfx_volume": 0.25},
    },
}


def check_style(name: str) -> str:
    """Normalized style name, or ValueError naming the valid ones."""
    style = str(name or DEFAULT_STYLE).strip().lower()
    if style not in STYLES:
        raise ValueError(
            f"unknown style '{name}' (use {', '.join(STYLES)})"
        )
    return style


def resolve_style(cfg: dict, override: str | None = None) -> str:
    """The style for a new run: --style wins over [style].preset."""
    return check_style(override or cfg.get("style", {}).get("preset", DEFAULT_STYLE))


def apply_style(cfg: dict, style: str) -> dict:
    """A copy of cfg with the preset's keys laid over [effects]/[editing].
    The input dict is never changed."""
    preset = STYLES[check_style(style)]
    out = dict(cfg)
    for section, values in preset.items():
        out[section] = {**cfg.get(section, {}), **values}
    return out
