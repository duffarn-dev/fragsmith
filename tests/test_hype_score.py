"""Tests for lib/hype_score.py: teamkills and suicides don't count as kills.

Run: python tests/test_hype_score.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import lib.assemble as assemble  # noqa: E402
from lib.edit_engine import _timing_plan  # noqa: E402
from lib.hype_score import (  # noqa: E402
    build_candidates,
    counts_as_kill,
    detect_won_clutches,
    is_teamkill,
)
from lib.models import EditDecision  # noqa: E402
from lib.rules_director import run_rules_director  # noqa: E402

FAILS: list[str] = []
TICKRATE = 64
FX = {
    "enabled": True, "headshot_punch": True, "kill_zoom": False,
    "headshot_micro_slowmo": True, "eased_ramps": False, "freeze_on_finish": True,
}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


TEAMS = {"me": "T", "mate1": "T", "mate2": "T", "mate3": "T", "e1": "CT", "e2": "CT"}


def kill(tick: int, rnd: int, attacker: str, victim: str,
         weapon: str = "ak47", headshot: bool = False) -> dict:
    return {
        "tick": tick, "round": rnd,
        "attacker_steamid": attacker, "attacker_name": attacker,
        "attacker_team": TEAMS.get(attacker, ""),
        "victim_steamid": victim, "victim_name": victim,
        "victim_team": TEAMS.get(victim, ""),
        "weapon": weapon, "headshot": headshot,
    }


def rnd(no: int, start: int, end: int, winner: str | None, roster: dict) -> dict:
    return {"round": no, "freeze_end_tick": start, "end_tick": end,
            "winner": winner, "roster": roster}


TEAMKILL_TICK = 2300
EVENTS = {
    "demo_path": "C:/demos/match1.dem", "map": "de_mirage", "tickrate": TICKRATE,
    "players": [{"steamid": s, "name": s} for s in TEAMS],
    "kills": [
        # round 5: two enemy kills with an AWP headshot on a teammate between
        kill(2000, 5, "me", "e1"),
        kill(TEAMKILL_TICK, 5, "me", "mate1", "awp", headshot=True),
        kill(2600, 5, "me", "e2", headshot=True),
        # round 6: a lone AWP headshot on a teammate (an enemy would be a clip)
        kill(4000, 6, "me", "mate2", "awp", headshot=True),
        # round 7: the teamkill leaves me alone against two, and I win
        kill(6100, 7, "me", "mate3"),
        kill(6200, 7, "me", "e1"),
        kill(6300, 7, "me", "e2"),
        # round 8: a suicide with my own grenade
        kill(8100, 8, "me", "me", "hegrenade"),
    ],
    "rounds": [
        rnd(5, 1000, 5000, None, {}),
        rnd(6, 3500, 5500, None, {}),
        rnd(7, 6000, 7000, "T", {"me": "T", "mate3": "T", "e1": "CT", "e2": "CT"}),
        rnd(8, 8000, 9000, None, {}),
    ],
}
CFG = {"clips": {}, "players": {"known_players": ["me"]}}


def test_rules() -> None:
    print("what counts as a kill:")
    ks = EVENTS["kills"]
    check("enemy kill counts", counts_as_kill(ks[0]) and not is_teamkill(ks[0]))
    check("teamkill is a teamkill", is_teamkill(ks[1]))
    check("teamkill doesn't count", not counts_as_kill(ks[1]))
    check("suicide isn't a teamkill", not is_teamkill(ks[-1]))
    check("suicide doesn't count", not counts_as_kill(ks[-1]))
    unknown = {**ks[1], "attacker_team": "", "victim_team": ""}
    check("unknown teams: not a teamkill", not is_teamkill(unknown))
    world = {**ks[0], "attacker_steamid": "", "attacker_team": ""}
    check("world death doesn't count", not counts_as_kill(world))


def test_candidates() -> None:
    print("multikill with a teamkill inside:")
    cands, flags, pov = build_candidates(EVENTS, CFG)
    by_round = {c.round: c for c in cands}
    check("POV is me", pov == "me", pov)

    c5 = by_round.get(5)
    check("round 5 is a clip", c5 is not None)
    if c5 is None:
        return
    check("round 5 is a 2K", c5.kill_count == 2, str(c5.kill_count))
    check("2K scores like a plain 2K", c5.hype_score == 3.0, str(c5.hype_score))
    check("no AWP from the teamkill", "awp" not in c5.weapons, str(c5.weapons))
    ticks = [k.tick for k in c5.kills]
    check("kill marks skip the teamkill", ticks == [2000, 2600], str(ticks))
    check("the teamkill footage stays in the clip",
          c5.start_tick < TEAMKILL_TICK < c5.end_tick)

    decision = EditDecision(
        clip_id=c5.clip_id, order=1, slowmo_segments=[], overlay_text=None,
        transition_in="cut", reasoning="test",
    )
    tp = _timing_plan(decision, c5, c5.duration_seconds, FX)
    tk_offset = (TEAMKILL_TICK - c5.start_tick) / TICKRATE
    near_tk = [t for t in tp["hs_marks"] if abs(t - tk_offset) < 0.5]
    check("no headshot punch at the teamkill", not near_tk, str(tp["hs_marks"]))
    micro_tk = [m for m in tp["micro"] if m[0] <= tk_offset <= m[1]]
    check("no micro slow-mo at the teamkill", not micro_tk, str(tp["micro"]))
    check("the real headshot still gets a punch", len(tp["hs_marks"]) == 1,
          str(tp["hs_marks"]))

    print("other rounds:")
    check("a lone teamkill makes no clip", 6 not in by_round,
          by_round[6].clip_id if 6 in by_round else "")
    check("a suicide makes no clip", 8 not in by_round)
    c7 = by_round.get(7)
    check("teamkilled mate still leaves me in a 1v2", detect_won_clutches(EVENTS)
          .get((7, "me")) == (2, 6100), str(detect_won_clutches(EVENTS)))
    check("round 7 is a 1v2 clutch 2K", c7 is not None and c7.kind == "clutch"
          and c7.clutch_vs == 2 and c7.kill_count == 2,
          f"{c7.kind} 1v{c7.clutch_vs} {c7.kill_count}K" if c7 else "missing")

    note = [f for f in flags if "teamkill" in f]
    check("flag names the left-out teamkills",
          len(note) == 1 and note[0].startswith("3 teamkill(s) left out")
          and "round 5 (mate1)" in note[0], str(note))


def test_overlay_and_report() -> None:
    print("overlay, stats and report:")
    cands, _, _ = build_candidates(EVENTS, CFG)
    plan = run_rules_director(cands, EVENTS, ["a.mp3"], "me")
    overlays = {d.clip_id: d.overlay_text for d in plan.decisions}
    r5 = next(c.clip_id for c in cands if c.round == 5)
    check("round 5 has no 3K overlay", overlays.get(r5) != "3K", str(overlays))

    stats = assemble.player_stats(EVENTS, "me")
    check("stats count enemy kills only", stats and stats["kills"] == 4,
          str(stats and stats["kills"]))

    by_id = {c.clip_id: c for c in cands}
    real_probe = assemble.probe_duration
    assemble.probe_duration = lambda _p: 10.0  # no film needed for the text
    try:
        with tempfile.TemporaryDirectory() as tmp:
            report = assemble.write_report(
                plan, by_id, [], [], {}, Path(tmp) / "final.mp4", Path(tmp),
                events=EVENTS,
            ).read_text(encoding="utf-8")
    finally:
        assemble.probe_duration = real_probe
    check("report.md lists the teamkills", "3 teamkill(s) left out" in report)
    check("report.md POV stats say 4 kills", "**POV stats:** 4 kills" in report)


if __name__ == "__main__":
    test_rules()
    test_candidates()
    test_overlay_and_report()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} — {', '.join(FAILS)}")
        sys.exit(1)
    print("All hype score tests OK")
