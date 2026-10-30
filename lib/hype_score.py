"""Steg 2: Regelbasert hype-scoring → kandidatklipp (Highlight-modeller).

Ingen LLM her — rene regler. Clutch-deteksjon bygges selv fra death-events +
roster per runde (demoparser2 har ingen ferdige clutch-flagg).

Justeringer av reglene skjer på bakgrunn av learnings_runtime.md
("Hype-scoring"-seksjonen), ikke ad hoc.
"""
from __future__ import annotations

from lib.models import Highlight, KillMark

SPECIAL_SINGLE_WEAPONS = {"knife", "knife_t", "bayonet", "taser", "zeus"}
AWP_WEAPONS = {"awp"}
DEAGLE_WEAPONS = {"deagle", "revolver"}


def build_candidates(
    events: dict,
    cfg: dict,
    player_override: str | None = None,
) -> tuple[list[Highlight], list[str], str]:
    """Returnerer (kandidater, flagg, valgt POV-steamid)."""
    flags: list[str] = []
    clips_cfg = cfg.get("clips", {})
    known = [str(p) for p in cfg.get("players", {}).get("known_players", [])]

    kills = [k for k in events["kills"] if k["attacker_steamid"]]
    present = {k["attacker_steamid"] for k in kills}

    # --- Velg POV-spiller ------------------------------------------------
    if player_override:
        pov_candidates = [player_override]
        if player_override not in present:
            flags.append(
                f"--player {player_override} har ingen kills i demoen — "
                f"sannsynlig feil steamid?"
            )
    else:
        pov_candidates = [p for p in known if p in present]
        if not pov_candidates:
            flags.append(
                "Ingen av known_players fra config.toml er i demoen — "
                "vurderer alle spillere i stedet. Sjekk kontoliste."
            )
            pov_candidates = sorted(present)

    clutches = detect_won_clutches(events)

    # Score alle POV-kandidater, velg spilleren med høyest totalscore
    best_pov = ""
    best_total = -1.0
    best_clips: list[Highlight] = []
    for pov in pov_candidates:
        clips = _clips_for_player(pov, kills, events, clutches, clips_cfg)
        total = sum(c.hype_score for c in clips)
        if total > best_total:
            best_pov, best_total, best_clips = pov, total, clips

    if len(pov_candidates) > 1 and not player_override:
        others = [p for p in pov_candidates if p != best_pov]
        flags.append(
            f"Flere kjente kontoer i demoen ({len(pov_candidates)}); valgte "
            f"{_name_of(best_pov, events)} ({best_pov}) på høyest hype-score. "
            f"Andre: {', '.join(others)}"
        )

    # --- Cap: behold ALLTID 3k+ og vunnede clutcher ----------------------
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
            f"{dropped} kandidat(er) under cap på {max_candidates} droppet "
            f"(laveste score, ingen 3k+/clutch)."
        )
    kept.sort(key=lambda c: c.hype_score, reverse=True)

    if not kept:
        flags.append(
            f"0 kandidatklipp for {best_pov} — for få kills eller for "
            f"strenge regler?"
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

    # Klump kills: samme runde + tick-gap < max_gap → samme klipp
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
        # Clutch/duell gjelder kun klipp som slutter ETTER at 1vX-situasjonen
        # oppsto — tidligere kills i samme runde er vanlige kills.
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
        if rnd:  # ikke vis forrige runde / etter runden
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
        # P1: per-kill-tidsstempler (offset i KILDE-tid fra klippstart).
        # Clamp defensivt til [0, duration] — start/end kan være klampet
        # til rundegrensene over.
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

    special_single = n == 1 and (
        weapons[0] in SPECIAL_SINGLE_WEAPONS
        or (weapons[0] in AWP_WEAPONS and cluster[0]["headshot"])
    )
    # Enkeltkills er kun med hvis clutch eller noe spesielt (kniv/AWP-hs)
    if n == 1 and not clutch_vs and not special_single:
        return None

    score = {1: 1.5, 2: 3.0, 3: 5.5, 4: 7.5}.get(n, 9.5 if n >= 5 else 0.0)

    if clutch_vs and clutch_vs >= 2:
        score += 2.0 + 0.5 * (clutch_vs - 2)  # 1v2=+2.0, 1v3=+2.5, ...
    elif clutch_vs == 1:
        score += 0.5  # vunnet 1v1-duell — litt drama, ikke clutch

    score += 1.5 * sum(1 for w in weapons if w in SPECIAL_SINGLE_WEAPONS)
    score += min(1.5, 0.5 * sum(1 for w in weapons if w in AWP_WEAPONS))
    score += 0.3 * sum(1 for w in weapons if w in DEAGLE_WEAPONS)

    if n >= 3 and headshots / n >= 0.75:
        score += 0.5
    if n >= 3 and (cluster[-1]["tick"] - cluster[0]["tick"]) <= 5 * tickrate:
        score += 0.5  # rask multikill

    if n >= 5:
        kind = "ace"
    elif clutch_vs and clutch_vs >= 2:
        kind = "clutch"
    elif n >= 3:
        kind = "multikill"
    else:
        kind = "highlight_single" if n == 1 else "multikill"

    return Highlight(
        clip_id="",  # settes av kaller
        start_tick=0,
        end_tick=0,
        player_steamid="",
        kill_count=n,
        kind=kind,
        hype_score=round(min(score, 10.0), 2),
        weapons=sorted(set(weapons)),
        headshots=headshots,
        clutch_vs=clutch_vs if clutch_vs else None,  # 1 = vunnet 1v1-duell
    )


def detect_won_clutches(events: dict) -> dict[tuple[int, str], tuple[int, int]]:
    """Finn vunnede clutcher: {(runde, steamid): (X, fra_tick)} for 1vX (X >= 1).

    Simulerer hvem som lever gjennom hver runde via roster-snapshot +
    death-events. Clutch = spillerens lag er nede i 1 mot >= 2 levende
    motstandere, OG spillerens lag vinner runden. X=1 er en vunnet
    1v1-DUELL — ikke clutch-kind, men Director skal slow-moe avslutningen.
    fra_tick er øyeblikket situasjonen oppsto: kills FØR det er ikke en del
    av clutchen/duellen (runde med to klipp skal ikke dobbelt-stemples).
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
            # Fallback: bygg roster fra team-feltene i rundens kills
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
        # Laget vant runden (sjekket over) — clutcheren kan ha dødd og
        # likevel "vunnet" (bombe eksploderer), så vi krever ikke overlevelse.
        if clutcher:
            result[(rnd["round"], clutcher)] = (clutch_vs, clutch_tick)
    return result
