"""Step 3: AI Director — Claude picks the final clips, order and pacing.

The only LLM step in the pipeline. Structured output via messages.parse() +
Pydantic (EditPlan). On validation/semantic errors: retry with the error
message sent back to the model, max 3 attempts, then DirectorError.

Everything is logged: the prompt to runs/<id>/02-edit-plan.prompt.txt, the
plan (incl. reasoning per clip) to 02-edit-plan.json.
"""
from __future__ import annotations

import json
from pathlib import Path

import anthropic

from lib.models import EditPlan, Highlight

MUSIC_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a"}


class DirectorError(RuntimeError):
    """The Director couldn't produce a valid plan."""


SYSTEM_PROMPT = """\
You are the AI Director for Fragsmith — an autonomous fragmovie generator for CS2.
You get a list of candidate clips (with a hype score from a rule-based filter)
and must create an edit plan for a fragmovie on behalf of the player.

Principles for a good fragmovie:
- Start strong (hook), build toward the craziest clip near the end (climax).
- Vary the pacing: no two long clips back to back.
- Use slow motion SPARINGLY — only on the most spectacular moment of a clip
  (the last kill of an ace, the end of a clutch). 0-2 segments per clip.
- Drop boring candidates without guilt — 4 good clips beat 8 mediocre ones.
  But: aces and big clutches (1v3+) should practically always be included.
- Overlay text short and in gaming slang ("3K", "1v4 CLUTCH", "ACE") — no
  text at all on weaker clips.
- transition_in: "cut" is the default; "fade" only into a change of tempo;
  "speed_ramp" where the clip starts in the action.
- intro_text/outro_text: short and clean (e.g. player name + map, "GG" etc.).

Effect control (optional, per clip):
- is_climax: set on at most ONE clip — the film's high point. That clip gets
  extra weight (dutch tilt on the last kill). If you leave it out, the last
  clip counts as the climax. Set it when the high point is NOT last.
- effects_on / effects_off: override individual effects where it lifts the film:
  "letterbox" (cinematic bars — default only on slow-mo/clutch),
  "freeze" (freeze frame on the last kill — default only on ace/4k),
  "kill_zoom" (deep zoom on the last kill — default on),
  "headshot_punch" (zoom punch on headshots — default on),
  "impact_split" (RGB flicker on big kills — default on),
  "kill_counter" (kill counter — default on for 3+ kills),
  "replay" (instant replay: the last kill rewinds and is shown in slow-mo
  from the VICTIM's point of view — default on the climax clip if it has 3+
  kills or a clutch; at most ONE clip can have replay in effects_on).
  The default rules are good: use this ONLY with a concrete reason (e.g.
  freeze on a 3k that ends the film, letterbox off on a hectic clip, replay
  on the film's single craziest kill). Never numeric parameters — only choices.

Briefly justify each choice in the reasoning field.
"""


def _build_prompt(
    candidates: list[Highlight],
    events: dict,
    music_files: list[str],
    pov_name: str,
) -> str:
    cand_json = json.dumps(
        [c.model_dump() for c in candidates], indent=2, ensure_ascii=False
    )
    rounds = events.get("rounds", [])
    return f"""\
Create an edit plan (EditPlan) for this demo.

Context:
- Player (POV): {pov_name}
- Map: {events.get('map') or 'unknown'}
- Number of rounds: {len(rounds)}
- Demo: {events.get('demo_path')}

Candidate clips (from rule-based hype scoring, 0-10):
{cand_json}

Available music tracks (pick ONE exact file name for music_track):
{json.dumps(music_files, indent=2, ensure_ascii=False)}

Requirements:
- Every candidate must EITHER have an EditDecision OR be in dropped_clip_ids.
- order is 1-indexed and contiguous (1, 2, 3, ...) in display order.
- slowmo_segments are in seconds relative to the clip's start and must lie
  within the clip's duration (duration_seconds). factor 0.2-0.5 for slow-mo.
- kills[] per candidate is read-only context: each kill with offset_s
  (seconds from clip start), weapon, headshot and victim. Use them to place
  slowmo_segments PRECISELY around the most spectacular kill (usually the
  last) instead of guessing — e.g. from ~0.5 s before to ~0.5 s after offset_s.
- clutch_vs=1 means a WON 1v1 DUEL: always add a short slow-mo
  (factor ~0.4) around the last kill in that clip — the end of the duel is the point.
- If SEVERAL candidates in the same round have clutch_vs >= 2, they are parts
  of the SAME clutch (a long pause between kills splits clips). Then ALL of
  them must be included, back to back in chronological order (ascending
  start_tick) — the last clip is the payoff and is NEVER dropped. Slow-mo
  belongs on the final kill in the LAST part.
- demo_path is set to the demo's path (above).
- is_climax/effects_on/effects_off are optional — leave them out where the
  default rules hold. The same effect can't be in both effects_on and effects_off.
"""


def _semantic_errors(
    plan: EditPlan, candidates: list[Highlight], music_files: list[str]
) -> list[str]:
    errors: list[str] = []
    by_id = {c.clip_id: c for c in candidates}

    used = [d.clip_id for d in plan.decisions]
    unknown = [c for c in used + plan.dropped_clip_ids if c not in by_id]
    if unknown:
        errors.append(f"Unknown clip_ids: {unknown}. Valid: {sorted(by_id)}")
    if len(set(used)) != len(used):
        errors.append("Duplicate clip_ids in decisions.")

    covered = set(used) | set(plan.dropped_clip_ids)
    missing = sorted(set(by_id) - covered)
    if missing:
        errors.append(
            f"Candidates neither chosen nor dropped: {missing}. "
            f"Every candidate must either have a decision or be in dropped_clip_ids."
        )

    orders = sorted(d.order for d in plan.decisions)
    if orders != list(range(1, len(orders) + 1)):
        errors.append(f"order must be 1..{len(orders)} without gaps, was: {orders}")

    for d in plan.decisions:
        cand = by_id.get(d.clip_id)
        if not cand:
            continue
        for seg in d.slowmo_segments:
            if not (0 <= seg.start_s < seg.end_s <= cand.duration_seconds + 0.5):
                errors.append(
                    f"{d.clip_id}: slowmo {seg.start_s}-{seg.end_s}s outside "
                    f"the clip's duration ({cand.duration_seconds}s)."
                )
            if not (0.1 <= seg.factor <= 1.0):
                errors.append(
                    f"{d.clip_id}: slowmo factor {seg.factor} outside 0.1-1.0."
                )

    if music_files and plan.music_track not in music_files:
        errors.append(
            f"music_track '{plan.music_track}' doesn't exist. Pick from the list."
        )

    # At most one climax clip; on/off can't overlap
    climax = [d.clip_id for d in plan.decisions if d.is_climax]
    if len(climax) > 1:
        errors.append(
            f"is_climax is set on {len(climax)} clips ({climax}) — at most one."
        )
    for d in plan.decisions:
        both = set(d.effects_on) & set(d.effects_off)
        if both:
            errors.append(
                f"{d.clip_id}: {sorted(both)} is in both effects_on and "
                f"effects_off — pick one."
            )
    replay_on = [d.clip_id for d in plan.decisions if "replay" in d.effects_on]
    if len(replay_on) > 1:
        errors.append(
            f"'replay' in effects_on on {len(replay_on)} clips ({replay_on}) "
            f"— at most one (one replay per film)."
        )

    # Split clutch (several clips from the same won clutch round): all must be
    # included, contiguous and in chronological order — the payoff is in the last.
    clutch_rounds: dict[int, list] = {}
    for c in candidates:
        if c.clutch_vs and c.clutch_vs >= 2:
            clutch_rounds.setdefault(c.round, []).append(c)
    order_by_id = {d.clip_id: d.order for d in plan.decisions}
    for rnd_no, group in clutch_rounds.items():
        if len(group) < 2:
            continue
        ids = [c.clip_id for c in sorted(group, key=lambda c: c.start_tick)]
        if any(cid not in order_by_id for cid in ids):
            errors.append(
                f"Round {rnd_no}: the clutch is split into several clips ({ids}) — "
                f"ALL must be included; the last clip is the payoff."
            )
            continue
        orders = [order_by_id[cid] for cid in ids]
        if orders != list(range(min(orders), min(orders) + len(orders))):
            errors.append(
                f"Round {rnd_no}: the clutch parts {ids} must come right after "
                f"each other in chronological order (order {orders})."
            )
    return errors


def list_music_files(music_dir: str | Path) -> list[str]:
    music_dir = Path(music_dir)
    if not music_dir.is_dir():
        return []
    return sorted(
        f.name for f in music_dir.iterdir() if f.suffix.lower() in MUSIC_EXTENSIONS
    )


def run_director(
    candidates: list[Highlight],
    events: dict,
    music_files: list[str],
    pov_name: str,
    run_dir: Path,
    model: str,
    max_retries: int = 3,
) -> EditPlan:
    client = anthropic.Anthropic()
    prompt = _build_prompt(candidates, events, music_files, pov_name)
    (run_dir / "02-edit-plan.prompt.txt").write_text(prompt, encoding="utf-8")

    messages: list[dict] = [{"role": "user", "content": prompt}]
    last_errors: list[str] = []

    for attempt in range(1, max_retries + 1):
        response = client.messages.parse(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=messages,
            output_format=EditPlan,
        )
        if response.stop_reason == "refusal":
            raise DirectorError("The model refused the request (refusal).")

        plan = response.parsed_output
        if plan is None:
            last_errors = ["The response couldn't be parsed into an EditPlan."]
        else:
            last_errors = _semantic_errors(plan, candidates, music_files)
            if not last_errors:
                plan.demo_path = events.get("demo_path", plan.demo_path)
                return plan

        raw_text = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
        messages.append({"role": "assistant", "content": raw_text or "(empty)"})
        messages.append(
            {
                "role": "user",
                "content": (
                    "Your plan had errors — fix them and return the whole plan "
                    "again:\n- " + "\n- ".join(last_errors)
                ),
            }
        )
        print(f"    Director attempt {attempt} failed: {len(last_errors)} error(s)")

    raise DirectorError(
        f"Gave up after {max_retries} attempts. Last errors: {last_errors}"
    )
