"""Step 2: rule-based hype scoring → candidate clips (Highlight models).

No LLM here, just rules. Clutch detection is built from death events +
the roster per round (demoparser2 has no ready-made clutch flags).

Rule changes are based on learnings_runtime.md (the "Hype scoring"
section), not ad hoc.
"""
from __future__ import annotations

from lib.models import Highlight, KillMark

SPECIAL_SINGLE_WEAPONS = {"knife", "knife_t", "bayonet", "taser", "zeus"}
AWP_WEAPONS = {"awp"}
LONG_SHOT_M = 35.0  # a kill from at least this far counts as a style point
CONTEXT_FLAGS = ("noscope", "through_smoke", "wallbang", "jumpshot", "blind")


def style_points(kill: dict) -> int:
    """How many style details a kill has (noscope, wallbang, through smoke,
    jumpshot, blind, long shot)."""
    points = sum(1 for flag in CONTEXT_FLAGS if kill.get(flag))
    return points + (1 if float(kill.get("distance_m") or 0) >= LONG_SHOT_M else 0)
DEAGLE_WEAPONS = {"deagle", "revolver"}


def build_candidates(
    events: dict,
    cfg: dict,
    player_override: str | None = None,
) -> tuple[list[Highlight], list[str], str]:
    """Returns (candidates, flags, chosen POV steamid)."""
    flags: list[str] = []
    clips_cfg = cfg.get("clips", {})
    known = [str(p) for p in cfg.get("players", {}).get("known_players", [])]

    kills = [k for k in events["kills"] if k["attacker_steamid"]]
    present = {k["attacker_steamid"] for k in kills}

    # --- Pick the POV player ---------------------------------------------
    if player_override:
        pov_candidates = [player_override]
        if player_override not in present:
            flags.append(
                f"--player {player_override} has no kills in the demo — "
                f"wrong SteamID?"
            )
    else:
        pov_candidates = [p for p in known if p in present]
        fallback = not pov_candidates
        if fallback:
            pov_candidates = sorted(present)

    clutches = detect_won_clutches(events)

    # Score all POV candidates, pick the player with the highest total score
    best_pov = ""
    best_total = -1.0
    best_clips: list[Highlight] = []
    for pov in pov_candidates:
        clips = _clips_for_player(pov, kills, events, clutches, clips_cfg)
        total = sum(c.hype_score for c in clips)
        if total > best_total:
            best_pov, best_total, best_clips = pov, total, clips

    if player_override:
        pass
    elif fallback:
        flags.append(
            "None of the known_players from config.toml are in the demo, so "
            f"the player with the best clips was picked: "
            f"{_name_of(best_pov, events)} ({best_pov}). Use --player "
            f"<steamid64> to film someone else."
        )
    elif len(pov_candidates) > 1:
        others = [p for p in pov_candidates if p != best_pov]
        flags.append(
            f"Several known accounts in the demo ({len(pov_candidates)}); picked "
            f"{_name_of(best_pov, events)} ({best_pov}) by highest hype score. "
            f"Others: {', '.join(others)}"
        )

    # --- Cap: ALWAYS keep 3k+ and won clutches ---------------------------
    max_candidates = int(clips_cfg.get("max_candidates", 25))
    must_keep = [c for c in best_clips if c.kill_count >= 3 or c.kind == "clutch"]
    rest = sorted(
        (c for c in best_clips if c not in must_keep),
        key=lambda c: c.hype_score,
        reverse=True,
    )
    kept = must_keep + rest
    if len(kept) > max_candidates:
        dropped = len(kept) - max_candidates
        kept = kept[:max_candidates]
        flags.append(
            f"{dropped} candidate(s) dropped by the cap of {max_candidates} "
            f"(lowest score, no 3k+/clutch)."
        )
    kept.sort(key=lambda c: c.hype_score, reverse=True)

    if not kept:
        flags.append(
            f"0 candidate clips for {best_pov} — too few kills or too "
            f"strict rules?"
        )
    return kept, flags, best_pov


def _name_of(steamid: str, events: dict) -> str:
    for p in events["players"]:
        if p["steamid"] == steamid:
            return p["name"]
    return steamid


def _clips_for_player(
    pov: str,
    kills: list[dict],
    events: dict,
    clutches: dict[tuple[int, str], tuple[int, int]],
    clips_cfg: dict,
) -> list[Highlight]:
    tickrate = events.get("tickrate", 64)
    pre = float(clips_cfg.get("pre_kill_seconds", 5.0))
    post = float(clips_cfg.get("post_kill_seconds", 3.0))
    max_gap_ticks = float(clips_cfg.get("max_gap_seconds", 15.0)) * tickrate

    my_kills = sorted(
        (k for k in kills if k["attacker_steamid"] == pov),
        key=lambda k: k["tick"],
    )

    # Group kills: same round + tick gap < max_gap → same clip
    clusters: list[list[dict]] = []
    for k in my_kills:
        if (
            clusters
            and k["round"] == clusters[-1][-1]["round"]
            and k["tick"] - clusters[-1][-1]["tick"] <= max_gap_ticks
        ):
            clusters[-1].append(k)
        else:
            clusters.append([k])

    rounds_by_no = {r["round"]: r for r in events.get("rounds", [])}

    clips: list[Highlight] = []
    for cluster in clusters:
        round_no = cluster[0]["round"]
        # Clutch/duel only applies to clips that end AFTER the 1vX situation
        # arose; earlier kills in the same round are ordinary kills.
        clutch_vs = None
        entry = clutches.get((round_no, pov))
        if entry and cluster[-1]["tick"] >= entry[1]:
            clutch_vs = entry[0]
        h = _score_cluster(cluster, clutch_vs, tickrate)
        if h is None:
            continue

        start = int(cluster[0]["tick"] - pre * tickrate)
        end = int(cluster[-1]["tick"] + post * tickrate)
        rnd = rounds_by_no.get(round_no)
        if rnd:  # don't show the previous round / after the round
            start = max(start, rnd["freeze_end_tick"])
            end = min(end, rnd["end_tick"] + int(2 * tickrate))
        start = max(start, 0)

        h.clip_id = f"r{round_no:02d}_{h.kind}_{len(clips):02d}"
        h.start_tick = start
        h.end_tick = end
        h.player_steamid = pov
        h.player_name = cluster[0]["attacker_name"]
        h.round = round_no
        h.duration_seconds = round((end - start) / tickrate, 1)
        # Per-kill timestamps (offset in SOURCE time from the clip start).
        # Clamp defensively to [0, duration], since start/end may have been
        # clamped to the round boundaries above.
        h.kills = [
            KillMark(
                tick=k["tick"],
                offset_s=round(
                    max(0.0, min((k["tick"] - start) / tickrate,
                                 h.duration_seconds)), 3
                ),
                weapon=k["weapon"],
                headshot=k["headshot"],
                victim_name=k["victim_name"],
                victim_steamid=str(k.get("victim_steamid", "") or ""),
                noscope=bool(k.get("noscope", False)),
                through_smoke=bool(k.get("through_smoke", False)),
                wallbang=bool(k.get("wallbang", False)),
                jumpshot=bool(k.get("jumpshot", False)),
                blind=bool(k.get("blind", False)),
                distance_m=float(k.get("distance_m", 0.0) or 0.0),
            )
            for k in cluster
        ]
        clips.append(h)
    return clips


def _score_cluster(
    cluster: list[dict], clutch_vs: int | None, tickrate: int
) -> Highlight | None:
    n = len(cluster)
    weapons = [k["weapon"].lower() for k in cluster]
    headshots = sum(1 for k in cluster if k["headshot"])

    first = cluster[0]
    special_single = n == 1 and (
        weapons[0] in SPECIAL_SINGLE_WEAPONS
        or (weapons[0] in AWP_WEAPONS and first["headshot"])
        # style kills worth a clip on their own; through smoke and wallbangs
        # are common enough that they need a headshot too
        or first.get("noscope") or first.get("jumpshot") or first.get("blind")
        or (first["headshot"] and (first.get("wallbang") or first.get("through_smoke")))
    )
    # Single kills are only included if clutch or something special (knife/AWP hs)
    if n == 1 and not clutch_vs and not special_single:
        return None

    score = {1: 1.5, 2: 3.0, 3: 5.5, 4: 7.5}.get(n, 9.5 if n >= 5 else 0.0)

    if clutch_vs and clutch_vs >= 2:
        score += 2.0 + 0.5 * (clutch_vs - 2)  # 1v2=+2.0, 1v3=+2.5, ...
    elif clutch_vs == 1:
        score += 0.5  # won 1v1 duel: a bit of drama, not a clutch

    score += 1.5 * sum(1 for w in weapons if w in SPECIAL_SINGLE_WEAPONS)
    score += min(1.5, 0.5 * sum(1 for w in weapons if w in AWP_WEAPONS))
    score += 0.3 * sum(1 for w in weapons if w in DEAGLE_WEAPONS)
    # style points, capped so a wallbang 2K never beats a clean 4K
    score += min(1.5, 0.5 * sum(style_points(k) for k in cluster))

    if n >= 3 and headshots / n >= 0.75:
        score += 0.5
    if n >= 3 and (cluster[-1]["tick"] - cluster[0]["tick"]) <= 5 * tickrate:
        score += 0.5  # fast multikill

    if n >= 5:
        kind = "ace"
    elif clutch_vs and clutch_vs >= 2:
        kind = "clutch"
    elif n >= 3:
        kind = "multikill"
    else:
        kind = "highlight_single" if n == 1 else "multikill"

    return Highlight(
        clip_id="",  # set by the caller
        start_tick=0,
        end_tick=0,
        player_steamid="",
        kill_count=n,
        kind=kind,
        hype_score=round(min(score, 10.0), 2),
        weapons=sorted(set(weapons)),
        headshots=headshots,
        clutch_vs=clutch_vs if clutch_vs else None,  # 1 = won 1v1 duel
    )


def detect_won_clutches(events: dict) -> dict[tuple[int, str], tuple[int, int]]:
    """Find won clutches: {(round, steamid): (X, from_tick)} for 1vX (X >= 1).

    Simulates who is alive through each round using the roster snapshot +
    death events. Clutch = the player's team is down to 1 against >= 2 living
    opponents, AND the player's team wins the round. X=1 is a won 1v1 DUEL:
    not the clutch kind, but the Director should slow-mo the finish.
    from_tick is the moment the situation arose: kills BEFORE that are not
    part of the clutch/duel (a round with two clips shouldn't be tagged twice).
    """
    result: dict[tuple[int, str], tuple[int, int]] = {}
    kills_by_round: dict[int, list[dict]] = {}
    for k in events["kills"]:
        kills_by_round.setdefault(k["round"], []).append(k)

    for rnd in events.get("rounds", []):
        roster: dict[str, str] = dict(rnd.get("roster") or {})
        winner = rnd.get("winner")
        deaths = sorted(kills_by_round.get(rnd["round"], []), key=lambda k: k["tick"])

        if not roster:
            # Fallback: build the roster from the team fields in the round's kills
            for k in deaths:
                if k["victim_steamid"] and k["victim_team"] in ("T", "CT"):
                    roster.setdefault(k["victim_steamid"], k["victim_team"])
                if k["attacker_steamid"] and k["attacker_team"] in ("T", "CT"):
                    roster.setdefault(k["attacker_steamid"], k["attacker_team"])

        if not winner or not roster:
            continue

        alive = dict(roster)
        clutcher: str | None = None
        clutch_vs = 0
        clutch_tick = 0
        for k in deaths:
            alive.pop(k["victim_steamid"], None)
            if clutcher is None:
                t_alive = [s for s, t in alive.items() if t == "T"]
                ct_alive = [s for s, t in alive.items() if t == "CT"]
                for last_team, last, opp in (
                    ("T", t_alive, ct_alive),
                    ("CT", ct_alive, t_alive),
                ):
                    if len(last) == 1 and len(opp) >= 1 and last_team == winner:
                        clutcher, clutch_vs = last[0], len(opp)
                        clutch_tick = k["tick"]
        # The team won the round (checked above). The clutcher may have died
        # and still "won" (bomb explodes), so we don't require survival.
        if clutcher:
            result[(rnd["round"], clutcher)] = (clutch_vs, clutch_tick)
    return result
