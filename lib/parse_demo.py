"""Step 1: parse a CS2 .dem → events dict (kills, rounds, players).

Uses demoparser2 (LaihoE). The output is a plain dict serialized to
runs/<id>/00-events.json. Column names in demoparser2 can differ a bit
between demo sources (Valve MM vs FACEIT), so columns are looked up with
fallbacks and error messages list the actual columns, so deviations can be
noted in learnings_runtime.md.

demoparser2 fails at random now and then: in testing, about 1 parse in 20
raised an exception (IllegalPathOp, VectorResizeFailure, ...) or crashed
the whole Python process, with both 0.41 and 0.42, on demos that parsed
fine the next time. parse_demo() therefore parses in a child process and
retries; a crash can't be caught in-process.
"""
from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from demoparser2 import DemoParser

TEAM_NAMES = {2: "T", 3: "CT"}
DEFAULT_TICKRATE = 64  # CS2 is 64 tick (subtick doesn't change the demo tickrate)
PARSE_ATTEMPTS = 3
PROJECT_DIR = Path(__file__).resolve().parent.parent


class DemoParseError(RuntimeError):
    """The demo couldn't be parsed or was missing expected events.

    retry=True when the error may be demoparser2's randomness rather than
    the demo itself."""

    def __init__(self, message: str, retry: bool = False) -> None:
        super().__init__(message)
        self.retry = retry


def _col(df, *candidates: str) -> str:
    """Find the first column name that exists in the dataframe."""
    for name in candidates:
        if name in df.columns:
            return name
    raise DemoParseError(
        f"None of the columns {candidates} were found in the event. "
        f"Available columns: {list(df.columns)} "
        f"(unknown demo format? note it in learnings_runtime.md)"
    )


def kill_context(row) -> dict:
    """Style details of a kill from the player_death event. Missing columns
    (older demos/parsers) and NaN give False/0: bool(NaN) would be True."""

    def num(key: str) -> float:
        try:
            v = float(row.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0
        return 0.0 if math.isnan(v) else v

    return {
        "noscope": num("noscope") > 0,
        "through_smoke": num("thrusmoke") > 0,
        "wallbang": num("penetrated") > 0,  # number of surfaces penetrated
        "jumpshot": num("attackerinair") > 0,
        "blind": num("attackerblind") > 0,
        "distance_m": round(num("distance"), 1),  # the CS2 event reports meters
    }


def _steamid(value) -> str:
    """Normalize a steamid (can be int/float/str, or NaN for world)."""
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


def parse_demo(dem_path: str | Path, attempts: int = PARSE_ATTEMPTS) -> dict:
    """Parse in a child process, retrying random demoparser2 failures."""
    dem_path = Path(dem_path)
    if not dem_path.exists():
        raise DemoParseError(f"Cannot find the demo file: {dem_path}")

    errors: list[str] = []
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "events.json"
            proc = subprocess.run(
                [sys.executable, "-m", "lib.parse_demo", str(dem_path), str(out)],
                cwd=PROJECT_DIR, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if out.exists():
                result = json.loads(out.read_text(encoding="utf-8"))
                if "events" in result:
                    return result["events"]
                errors.append(result["error"])
                if not result["retry"]:
                    break
            else:
                errors.append(f"the parser crashed (exit code {proc.returncode})")
        if attempt < attempts:
            print(f"    parse attempt {attempt} failed ({errors[-1]}), retrying ...")
    suffix = f" (after {len(errors)} attempts)" if len(errors) > 1 else ""
    raise DemoParseError(errors[-1] + suffix)


def parse_demo_in_process(dem_path: str | Path) -> dict:
    dem_path = Path(dem_path)
    if not dem_path.exists():
        raise DemoParseError(f"Cannot find the demo file: {dem_path}")

    parser = DemoParser(str(dem_path))

    header = {}
    try:
        header = parser.parse_header()
    except Exception as exc:  # the header is nice to have, not critical
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
            f"(corrupt or unsupported demo?)",
            retry=True,
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
                **kill_context(row),
            }
        )

    # --- Rounds --------------------------------------------------------
    rounds = _parse_rounds(parser)
    if not rounds:
        # round_end can come back empty at random (see the module docstring)
        # without raising, so the process-level retry wouldn't catch it
        rounds = _parse_rounds(DemoParser(str(dem_path)))

    # Fallback: round number from round boundaries if total_rounds_played was missing
    if rounds and any(k["round"] == 0 for k in kills):
        for k in kills:
            if k["round"] == 0:
                k["round"] = _round_for_tick(k["tick"], rounds)

    # The last round can be missing round_end (surrender/disconnect).
    # Synthesize a round without a winner so clips there are still clamped
    # and counted.
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
                "winner": "",  # unknown, clutch detection skips the round
                "roster": {},
            }
        )

    # --- Roster per round (for clutch detection in step 2) --------------
    _attach_rosters(parser, rounds)

    # --- Players ---------------------------------------------------------
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
                "roster": {},  # filled in by _attach_rosters
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
    """Snapshot of who is on T/CT right after freeze time ends in each round."""
    if not rounds:
        return
    snapshot_ticks = [r["freeze_end_tick"] + 8 for r in rounds]
    try:
        df = parser.parse_ticks(["team_num"], ticks=snapshot_ticks)
    except Exception:
        return  # step 2 falls back to team info from kills
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
    # fill in from kills (also covers parse_player_info failing)
    for k in kills:
        for sid, name in (
            (k["attacker_steamid"], k["attacker_name"]),
            (k["victim_steamid"], k["victim_name"]),
        ):
            if sid and sid not in players:
                players[sid] = name
    return [{"steamid": sid, "name": name} for sid, name in players.items()]


def _child_main(dem_path: str, out_path: str) -> int:
    """Entry point for parse_demo's child process: writes {"events": ...}
    or {"error": ..., "retry": ...} to out_path."""
    try:
        result = {"events": parse_demo_in_process(dem_path)}
    except DemoParseError as exc:
        result = {"error": str(exc), "retry": exc.retry}
    except Exception as exc:  # noqa: BLE001 — demoparser2's random errors land here
        result = {"error": f"{type(exc).__name__}: {exc}", "retry": True}
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(_child_main(sys.argv[1], sys.argv[2]))
