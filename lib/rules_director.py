"""Step 3 without an LLM: a rule-based Director.

Used when there's no ANTHROPIC_API_KEY, when [director].mode = "rules", or
as a fallback when Claude fails in "auto" mode. It follows the same
principles as the AI Director's prompt, just with fixed rules:

- aces, 3k+ and won clutches (1v2+) are always included
- other clips are included if they score well enough, up to MAX_CLIPS
- the best clip comes last (climax), the second best opens (hook), and the
  rest build up in rising score order
- slow-mo only on the last kill of the climax and other big clips
- overlay text only on big clips ("ACE", "4K", "1v3 CLUTCH")

The plan is checked by the same validation as Claude's plan, so the rest
of the pipeline can't tell the difference.
"""
from __future__ import annotations

import zlib
from pathlib import Path

from lib.director import DirectorError, semantic_errors
from lib.models import EditDecision, EditPlan, Highlight, SlowmoSegment

MAX_CLIPS = 8       # more than this gets long and repetitive
MIN_CLIPS = 3       # fill up to this even with weaker clips, if there are any
MIN_SCORE = 3.5     # optional clips need at least this: a plain 2k (3.0) isn't
                    # enough, it needs headshots, an AWP, speed or the like


def _must_keep(c: Highlight) -> bool:
    return c.kill_count >= 3 or bool(c.clutch_vs and c.clutch_vs >= 2)


def _units(candidates: list[Highlight]) -> list[list[Highlight]]:
    """Group candidates into units that are placed as one block.

    A won clutch that was split into several clips (a long pause between
    kills) is one unit, in chronological order, with the payoff last.
    Everything else is a unit of one clip.
    """
    clutch_rounds: dict[int, list[Highlight]] = {}
    for c in candidates:
        if c.clutch_vs and c.clutch_vs >= 2:
            clutch_rounds.setdefault(c.round, []).append(c)
    grouped = {
        c.clip_id for group in clutch_rounds.values() if len(group) > 1
        for c in group
    }
    units = [
        sorted(group, key=lambda c: c.start_tick)
        for group in clutch_rounds.values() if len(group) > 1
    ]
    units += [[c] for c in candidates if c.clip_id not in grouped]
    return units


def _unit_score(unit: list[Highlight]) -> float:
    return max(c.hype_score for c in unit)


def _select(units: list[list[Highlight]]) -> list[list[Highlight]]:
    selected = [u for u in units if any(_must_keep(c) for c in u)]
    rest = sorted(
        (u for u in units if u not in selected), key=_unit_score, reverse=True
    )

    def n_clips() -> int:
        return sum(len(u) for u in selected)

    for u in rest:
        if _unit_score(u) >= MIN_SCORE and n_clips() + len(u) <= MAX_CLIPS:
            selected.append(u)
    for u in rest:
        if n_clips() >= MIN_CLIPS:
            break
        if u not in selected:
            selected.append(u)
    return selected


def _order(selected: list[list[Highlight]]) -> list[list[Highlight]]:
    """Hook first, climax last, rising score in between. Ties go to the
    later round, so a match's late heroics end the film."""
    ranked = sorted(
        selected, key=lambda u: (_unit_score(u), u[0].round), reverse=True
    )
    if len(ranked) < 3:
        return list(reversed(ranked))
    climax, hook, *middle = ranked
    middle.sort(key=lambda u: (_unit_score(u), u[0].round))
    return [hook, *middle, climax]


def _overlay(c: Highlight, is_payoff: bool) -> str | None:
    if c.kind == "ace":
        return "ACE"
    if is_payoff and c.clutch_vs and c.clutch_vs >= 2:
        return f"1v{c.clutch_vs} CLUTCH"
    if c.kill_count >= 3:
        return f"{c.kill_count}K"
    return None


def _slowmo(c: Highlight, is_climax: bool, is_payoff: bool) -> list[SlowmoSegment]:
    """One slow-mo segment around the last kill, on big clips only."""
    if not c.kills or not is_payoff:
        return []
    big = (
        c.kind in ("ace", "clutch")
        or c.kill_count >= 4
        or c.clutch_vs == 1  # a won 1v1 duel: the finish is the point
    )
    if not (is_climax or big):
        return []
    pre, post, factor = (0.6, 0.6, 0.35) if is_climax else (0.5, 0.5, 0.4)
    t = c.kills[-1].offset_s
    start = round(max(0.0, t - pre), 2)
    end = round(min(c.duration_seconds, t + post), 2)
    if end - start < 0.2:
        return []
    return [SlowmoSegment(start_s=start, end_s=end, factor=factor)]


def _describe(c: Highlight) -> str:
    if c.kind == "ace":
        return "ace"
    if c.clutch_vs and c.clutch_vs >= 2:
        return f"1v{c.clutch_vs} clutch"
    if c.clutch_vs == 1:
        return "won 1v1 duel"
    if c.kill_count >= 2:
        return f"{c.kill_count}k"
    return f"single kill ({', '.join(c.weapons) or 'special'})"


def _pick_music(music_files: list[str], demo_path: str) -> str:
    """Deterministic but varied: the same demo always gets the same track,
    different demos spread across the folder. Override with finish --music."""
    if not music_files:
        return ""
    key = Path(demo_path or "demo").name.encode("utf-8")
    return music_files[zlib.crc32(key) % len(music_files)]


def run_rules_director(
    candidates: list[Highlight],
    events: dict,
    music_files: list[str],
    pov_name: str,
) -> EditPlan:
    if not candidates:
        raise DirectorError("No candidate clips to build a plan from.")

    ordered = _order(_select(_units(candidates)))
    n_units = len(ordered)

    decisions: list[EditDecision] = []
    for ui, unit in enumerate(ordered):
        is_climax_unit = ui == n_units - 1
        if is_climax_unit:
            role = "climax (highest score)"
        elif ui == 0 and n_units >= 3:
            role = "opener (second-highest score)"
        else:
            role = "build-up"
        for pi, c in enumerate(unit):
            is_payoff = pi == len(unit) - 1
            slowmo = _slowmo(c, is_climax_unit and is_payoff, is_payoff)
            reason = f"Rules: {_describe(c)}, hype {c.hype_score:.1f} — {role}"
            if len(unit) > 1:
                reason += f", clutch part {pi + 1}/{len(unit)}"
            if slowmo:
                reason += ", slow-mo on the last kill"
            decisions.append(
                EditDecision(
                    clip_id=c.clip_id,
                    order=len(decisions) + 1,
                    slowmo_segments=slowmo,
                    overlay_text=_overlay(c, is_payoff),
                    transition_in="cut",
                    reasoning=reason + ".",
                )
            )

    used = {d.clip_id for d in decisions}
    demo_path = str(events.get("demo_path", ""))
    plan = EditPlan(
        demo_path=demo_path,
        music_track=_pick_music(music_files, demo_path),
        intro_text=(pov_name or "Fragsmith").upper(),
        outro_text="GG",
        decisions=decisions,
        dropped_clip_ids=[c.clip_id for c in candidates if c.clip_id not in used],
    )
    errors = semantic_errors(plan, candidates, music_files)
    if errors:
        # a bug in the rules, not something the user can fix
        raise DirectorError(f"The rule-based plan is invalid: {errors}")
    return plan
