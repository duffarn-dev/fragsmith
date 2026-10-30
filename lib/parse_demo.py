"""Steg 1: Parse CS2 .dem → events-struktur (kills, runder, spillere).

Bruker demoparser2 (LaihoE). Output er en ren dict som serialiseres til
runs/<id>/00-events.json. Kolonnenavn i demoparser2 kan variere litt mellom
demo-kilder (Valve MM vs FACEIT) — derfor slås kolonner opp med fallbacks og
feilmeldinger lister faktiske kolonner, så avvik kan legges i
learnings_runtime.md.
"""
from __future__ import annotations

import math
from pathlib import Path

from demoparser2 import DemoParser

TEAM_NAMES = {2: "T", 3: "CT"}
DEFAULT_TICKRATE = 64  # CS2 er 64 tick (subtick endrer ikke demo-tickrate)


class DemoParseError(RuntimeError):
    """Demoen kunne ikke parses / manglet forventede events."""


def _col(df, *candidates: str) -> str:
    """Finn første kolonnenavn som finnes i dataframen."""
    for name in candidates:
        if name in df.columns:
            return name
    raise DemoParseError(
        f"None of the columns {candidates} were found in the event. "
        f"Available columns: {list(df.columns)} "
        f"(unknown demo format? note it in learnings_runtime.md)"
    )


def _steamid(value) -> str:
    """Normaliser steamid (kan komme som int/float/str, eller NaN for world)."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    try:
        return str(int(value))
    except (ValueError, TypeError):
        return str(value)


def _team_name(value) -> str:
    try:
        return TEAM_NAMES.get(int(value), "")
    except (ValueError, TypeError):
        return str(value) if value else ""


def parse_demo(dem_path: str | Path) -> dict:
    dem_path = Path(dem_path)
    if not dem_path.exists():
        raise DemoParseError(f"Cannot find the demo file: {dem_path}")

    parser = DemoParser(str(dem_path))

    header = {}
    try:
        header = parser.parse_header()
    except Exception as exc:  # header er kjekt, men ikke kritisk
        header = {"_header_error": str(exc)}

    # --- Kills ---------------------------------------------------------
    try:
        deaths = parser.parse_event(
            "player_death",
            player=["team_num"],
            other=["total_rounds_played", "is_warmup_period"],
        )
    except Exception as exc:
        raise DemoParseError(
            f"Couldn't parse player_death events: {exc} "
            f"(corrupt or unsupported demo?)"
        ) from exc

    if len(deaths) == 0:
        raise DemoParseError(
            "0 player_death events found — probably a parse error or an "
            "empty/corrupt demo. Check the demo source."
        )

    if "is_warmup_period" in deaths.columns:
        deaths = deaths[deaths["is_warmup_period"] != True]  # noqa: E712

    tick_c = _col(deaths, "tick")
    att_sid_c = _col(deaths, "attacker_steamid")
    att_name_c = _col(deaths, "attacker_name")
    vic_sid_c = _col(deaths, "user_steamid", "victim_steamid")
    vic_name_c = _col(deaths, "user_name", "victim_name")
    weapon_c = _col(deaths, "weapon")

    deaths = deaths.sort_values(tick_c)

    kills = []
    for _, row in deaths.iterrows():
        round_no = 0
        if "total_rounds_played" in deaths.columns:
            try:
                round_no = int(row["total_rounds_played"]) + 1
            except (ValueError, TypeError):
                round_no = 0
        kills.append(
            {
                "tick": int(row[tick_c]),
                "round": round_no,
                "attacker_steamid": _steamid(row.get(att_sid_c)),
                "attacker_name": str(row.get(att_name_c) or ""),
                "attacker_team": _team_name(row.get("attacker_team_num")),
                "victim_steamid": _steamid(row.get(vic_sid_c)),
                "victim_name": str(row.get(vic_name_c) or ""),
                "victim_team": _team_name(row.get("user_team_num")),
                "weapon": str(row.get(weapon_c) or ""),
                "headshot": bool(row.get("headshot", False)),
            }
        )

    # --- Runder --------------------------------------------------------
    rounds = _parse_rounds(parser)

    # Fallback: runde-nummer fra rundegrenser hvis total_rounds_played manglet
    if rounds and any(k["round"] == 0 for k in kills):
        for k in kills:
            if k["round"] == 0:
                k["round"] = _round_for_tick(k["tick"], rounds)

    # Siste runde kan mangle round_end (surrender/disconnect) — syntetiser
    # en runde uten vinner så klipp der fortsatt klampes og telles.
    max_kill_round = max((k["round"] for k in kills), default=0)
    while rounds and max_kill_round > rounds[-1]["round"]:
        prev = rounds[-1]
        last_tick = max(
            (k["tick"] for k in kills if k["round"] == prev["round"] + 1),
            default=prev["end_tick"],
        )
        rounds.append(
            {
                "round": prev["round"] + 1,
                "start_tick": prev["end_tick"],
                "freeze_end_tick": prev["end_tick"],
                "end_tick": last_tick + 10 * DEFAULT_TICKRATE,
                "winner": "",  # ukjent — clutch-deteksjon hopper over runden
                "roster": {},
            }
        )

    # --- Roster per runde (for clutch-deteksjon i steg 2) ---------------
    _attach_rosters(parser, rounds)

    # --- Spillere --------------------------------------------------------
    players = _parse_players(parser, kills)

    return {
        "demo_path": str(dem_path),
        "map": header.get("map_name", ""),
        "tickrate": DEFAULT_TICKRATE,
        "header": {k: str(v) for k, v in header.items()},
        "players": players,
        "rounds": rounds,
        "kills": kills,
    }


def _parse_rounds(parser: DemoParser) -> list[dict]:
    try:
        ends = parser.parse_event("round_end")
    except Exception:
        return []
    if len(ends) == 0:
        return []

    tick_c = _col(ends, "tick")
    ends = ends.sort_values(tick_c)

    starts: list[int] = []
    try:
        st = parser.parse_event("round_start")
        starts = sorted(int(t) for t in st[_col(st, "tick")])
    except Exception:
        pass

    freeze_ends: list[int] = []
    try:
        fe = parser.parse_event("round_freeze_end")
        freeze_ends = sorted(int(t) for t in fe[_col(fe, "tick")])
    except Exception:
        pass

    rounds = []
    prev_end = 0
    for i, (_, row) in enumerate(ends.iterrows(), start=1):
        end_tick = int(row[tick_c])
        start_tick = max(
            [t for t in starts if prev_end <= t < end_tick], default=prev_end
        )
        freeze_end = max(
            [t for t in freeze_ends if start_tick <= t < end_tick],
            default=start_tick,
        )
        rounds.append(
            {
                "round": i,
                "start_tick": start_tick,
                "freeze_end_tick": freeze_end,
                "end_tick": end_tick,
                "winner": _winner_name(row.get("winner")),
                "roster": {},  # fylles av _attach_rosters
            }
        )
        prev_end = end_tick
    return rounds


def _winner_name(value) -> str:
    if value is None:
        return ""
    try:
        return TEAM_NAMES.get(int(value), str(value))
    except (ValueError, TypeError):
        s = str(value).upper()
        return s if s in ("T", "CT") else str(value)


def _round_for_tick(tick: int, rounds: list[dict]) -> int:
    for r in rounds:
        if r["start_tick"] <= tick <= r["end_tick"]:
            return r["round"]
    return 0


def _attach_rosters(parser: DemoParser, rounds: list[dict]) -> None:
    """Snapshot av hvem som er på T/CT rett etter freeze-slutt i hver runde."""
    if not rounds:
        return
    snapshot_ticks = [r["freeze_end_tick"] + 8 for r in rounds]
    try:
        df = parser.parse_ticks(["team_num"], ticks=snapshot_ticks)
    except Exception:
        return  # steg 2 faller tilbake til team-info fra kills
    tick_c = _col(df, "tick")
    sid_c = _col(df, "steamid")
    for r, snap_tick in zip(rounds, snapshot_ticks):
        rows = df[df[tick_c] == snap_tick]
        roster: dict[str, str] = {}
        for _, row in rows.iterrows():
            team = _team_name(row.get("team_num"))
            sid = _steamid(row.get(sid_c))
            if team in ("T", "CT") and sid:
                roster[sid] = team
        r["roster"] = roster


def _parse_players(parser: DemoParser, kills: list[dict]) -> list[dict]:
    players: dict[str, str] = {}
    try:
        info = parser.parse_player_info()
        sid_c = _col(info, "steamid")
        name_c = _col(info, "name")
        for _, row in info.iterrows():
            sid = _steamid(row.get(sid_c))
            if sid:
                players[sid] = str(row.get(name_c) or "")
    except Exception:
        pass
    # suppler fra kills (dekker også hvis parse_player_info feilet)
    for k in kills:
        for sid, name in (
            (k["attacker_steamid"], k["attacker_name"]),
            (k["victim_steamid"], k["victim_name"]),
        ):
            if sid and sid not in players:
                players[sid] = name
    return [{"steamid": sid, "name": name} for sid, name in players.items()]
