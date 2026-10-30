"""Delte Pydantic-modeller for kritiske beslutninger (plan seksjon 7)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class KillMark(BaseModel):
    """Ett drap inne i et klipp (P1). offset_s er sekunder fra klippets
    start_tick — i KILDE-tid; effekter må mappe via source_to_output_time
    i edit_engine når slow-mo/ramper har strukket tidslinja."""

    tick: int
    offset_s: float
    weapon: str
    headshot: bool
    victim_name: str
    victim_steamid: str = ""  # P17: trengs for offer-POV-render (schema 1.2)


class Highlight(BaseModel):
    """Ett kandidatklipp fra hype-scoringen (steg 2)."""

    clip_id: str
    start_tick: int
    end_tick: int
    player_steamid: str
    kill_count: int
    kind: Literal["multikill", "clutch", "ace", "highlight_single", "flick"]
    hype_score: float  # 0-10
    schema_version: str = "1.2"  # 1.1: + kills (P1) · 1.2: + victim_steamid (P17)

    # Kontekst til AI Director (utover planens minimumsfelter)
    round: int = 0
    player_name: str = ""
    weapons: list[str] = []
    headshots: int = 0
    clutch_vs: Optional[int] = None  # 1vX — vunnet clutch (>=2) eller 1v1-duell (1)
    duration_seconds: float = 0.0
    kills: list[KillMark] = []  # P1: per-kill-tidsstempler, i klipp-rekkefølge


class SlowmoSegment(BaseModel):
    """Ett slow-motion-segment, relativt til klippets start.

    Planen spesifiserte tuple[float, float, float]; navngitte felt valgt fordi
    structured outputs-skjemaet ikke støtter tuple-constraints godt.
    """

    start_s: float
    end_s: float
    factor: float  # avspillingshastighet: 0.25 = kraftig slow-mo, 1.0 = normal


# P18: effekter Director kan velge/overstyre per klipp. Kun VALG — aldri
# tallparametre (de styres av config, jf. plan.md: LLM kun i steg 3).
EffectName = Literal[
    "letterbox", "freeze", "kill_zoom",
    "headshot_punch", "impact_split", "kill_counter",
    "replay",  # P19: instant replay av siste drap fra offer-POV (maks ett klipp)
]


class EditDecision(BaseModel):
    """Directors beslutning for ett klipp."""

    clip_id: str
    order: int
    slowmo_segments: list[SlowmoSegment]
    overlay_text: Optional[str]  # f.eks. "1v4 CLUTCH"
    transition_in: Literal["cut", "fade", "speed_ramp"]
    reasoning: str  # Director må begrunne valget

    # P18: alle default av/tomme → deterministisk oppførsel uendret når
    # Director utelater dem. Config-flagg er fortsatt master-bryter:
    # effects_on kan ikke slå på noe brukeren har skrudd av globalt.
    is_climax: bool = False  # filmens høydepunkt (dutch tilt m.m.); maks ett
    effects_on: list[EffectName] = []   # tving PÅ utover standardreglene
    effects_off: list[EffectName] = []  # skru AV for dette klippet


class EditPlan(BaseModel):
    """Hele redigeringsplanen fra AI Director."""

    demo_path: str
    music_track: str
    intro_text: str
    outro_text: str
    decisions: list[EditDecision]
    dropped_clip_ids: list[str]
    schema_version: str = "1.1"  # 1.1: + is_climax/effects_on/effects_off (P18)
