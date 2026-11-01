"""Shared Pydantic models for the pipeline's critical decisions."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class KillMark(BaseModel):
    """A single kill inside a clip. offset_s is seconds from the clip's
    start_tick, in SOURCE time; effects must map it through
    source_to_output_time in edit_engine once slow-mo/ramps have stretched
    the timeline."""

    tick: int
    offset_s: float
    weapon: str
    headshot: bool
    victim_name: str
    victim_steamid: str = ""  # needed for the victim-POV render (schema 1.2)


class Highlight(BaseModel):
    """A candidate clip from hype scoring (step 2)."""

    clip_id: str
    start_tick: int
    end_tick: int
    player_steamid: str
    kill_count: int
    kind: Literal["multikill", "clutch", "ace", "highlight_single", "flick"]
    hype_score: float  # 0-10
    schema_version: str = "1.2"  # 1.1: + kills · 1.2: + victim_steamid

    # Context for the AI Director
    round: int = 0
    player_name: str = ""
    weapons: list[str] = []
    headshots: int = 0
    clutch_vs: Optional[int] = None  # 1vX: won clutch (>=2) or 1v1 duel (1)
    duration_seconds: float = 0.0
    kills: list[KillMark] = []  # per-kill timestamps, in clip order


class SlowmoSegment(BaseModel):
    """A slow-motion segment, relative to the clip's start.

    Named fields instead of tuple[float, float, float] because the
    structured outputs schema doesn't handle tuple constraints well.
    """

    start_s: float
    end_s: float
    factor: float  # playback speed: 0.25 = heavy slow-mo, 1.0 = normal


# Effects the Director can pick/override per clip. CHOICES only, never
# numeric parameters (those come from config; the LLM is only used in step 3).
EffectName = Literal[
    "letterbox", "freeze", "kill_zoom",
    "headshot_punch", "impact_split", "kill_counter",
    "replay",  # instant replay of the last kill from the victim's POV (max one clip)
]


class EditDecision(BaseModel):
    """The Director's decision for one clip."""

    clip_id: str
    order: int
    slowmo_segments: list[SlowmoSegment]
    overlay_text: Optional[str]  # e.g. "1v4 CLUTCH"
    transition_in: Literal["cut", "fade", "speed_ramp"]
    reasoning: str  # the Director must justify its choice

    # All default to off/empty, so behavior is deterministic and unchanged
    # when the Director leaves them out. Config flags are still the master
    # switch: effects_on can't enable anything the user turned off globally.
    is_climax: bool = False  # the film's high point (dutch tilt etc.); max one
    effects_on: list[EffectName] = []   # force ON beyond the default rules
    effects_off: list[EffectName] = []  # turn OFF for this clip


class EditPlan(BaseModel):
    """The full edit plan from the AI Director."""

    demo_path: str
    music_track: str
    intro_text: str
    outro_text: str
    decisions: list[EditDecision]
    dropped_clip_ids: list[str]
    schema_version: str = "1.2"  # 1.1: + is_climax/effects_on/effects_off · 1.2: + style
    # Set by `run` after the Director (whatever the Director put here is
    # overwritten). Older plans have none and read as "custom".
    style: str = "custom"
