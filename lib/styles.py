"""Style presets: one word for a tuned set of effects.

A preset overrides only the keys it names in [effects]/[editing]/[render];
everything else keeps the user's value. "custom" overrides nothing, so the
config is used exactly as written.

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
            "kill_tags": False,
            "lowerthird": True,
            "animated_overlays": True,
            "kinetic_text": False,
            "impact_split": False,
            "instant_replay": False,
            "transitions": [],
        },
        "editing": {"sfx": False, "riser": False, "drop_gap": False, "drop_boom": False},
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
            "kill_tags": True,
            "lowerthird": True,
            "animated_overlays": True,
            "kinetic_text": True,
            "impact_split": True,
            "instant_replay": True,
            "transitions": ["whip", "zoom", "glitch"],
        },
        "editing": {
            "sfx": True,
            "sfx_volume": 0.45,
            "drop_gap": True,
            "drop_boom": True,
        },
        "render": {"motion_blur": "climax"},
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
            "kill_tags": False,
            "lowerthird": True,
            "animated_overlays": True,
            "kinetic_text": False,
            "impact_split": False,
            "instant_replay": True,
            # a push, never a whip or a glitch
            "transitions": ["zoom"],
        },
        # the silence, but no boom: it's a film, not a club
        "editing": {
            "sfx": True,
            "sfx_volume": 0.25,
            "drop_gap": True,
            "drop_boom": False,
        },
        "render": {"motion_blur": "climax"},
    },
}


def _on_top_of(base: str, **sections: dict) -> dict:
    """A style pack: another preset with some switches changed, so it cuts
    like its base and only looks different."""
    pack = {section: dict(values) for section, values in STYLES[base].items()}
    for section, values in sections.items():
        pack[section] = {**pack.get(section, {}), **values}
    return pack


# Style packs: the hype or cinematic cut with a look of its own, made from the
# text looks, the glow and the impact shake (all off in the three above)
STYLES["neon"] = _on_top_of("hype", effects={
    "intro_hook_style": "velocity",
    "text_color": "cyan",
    "text_animation": "glitch",
    "glow": 0.5,
    "impact_shake": True,
    "wobble": False,  # the shake on the kills is the movement
    "grain": 0.0,
    "transitions": ["glitch", "whip"],
})
STYLES["rage"] = _on_top_of("hype", effects={
    "intensity": 1.6,
    "intro_hook_style": "velocity",
    "intro_hook_kills": 6,
    "text_color": "red",
    "impact_shake": True,
    "impact_shake_strength": 1.4,
    "wobble": False,
    "kill_tilt_degrees": 26.0,
    "transitions": ["whip"],
})
# The user's Matrix idea: bullet time on the climax kill, and the climax clip
# filmed in third person until the fight
STYLES["matrix"] = _on_top_of("hype", effects={
    "replay_style": "bullet_time",
    "instant_replay": True,
    "text_color": "lime",
    "text_animation": "glitch",
    "glow": 0.3,
    "wobble": False,
    "transitions": ["glitch", "zoom"],
}, render={"cinematic_approach": "climax"})
STYLES["dream"] = _on_top_of("cinematic", effects={
    "glow": 0.8,
    "kinetic_text": True,
    "text_color": "gold",
    "text_animation": "pop",
    "grain": 0.3,
})


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
