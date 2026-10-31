"""Tests for lib/rules_director.py: the edit plan without an LLM.

Run: python tests/test_rules_director.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.director import semantic_errors  # noqa: E402
from lib.models import Highlight, KillMark  # noqa: E402
from lib.rules_director import MAX_CLIPS, run_rules_director  # noqa: E402

FAILS: list[str] = []
MUSIC = ["a.mp3", "b.mp3", "c.mp3"]
EVENTS = {"demo_path": "C:/demos/match1.dem", "map": "de_inferno", "rounds": []}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def hl(
    cid: str, score: float, n_kills: int, rnd: int, dur: float = 10.0,
    start: int = 0, **kw,
) -> Highlight:
    kills = [
        KillMark(
            tick=start + 64 * (3 + i), offset_s=3.0 + i, weapon="ak47",
            headshot=False, victim_name="bot", victim_steamid="765x",
        )
        for i in range(n_kills)
    ]
    return Highlight(
        clip_id=cid, start_tick=start, end_tick=start + int(dur * 64),
        player_steamid="765me", kill_count=n_kills,
        kind=kw.pop("kind", "multikill"), hype_score=score,
        duration_seconds=dur, kills=kills, player_name="Tester", round=rnd,
        **kw,
    )


def test_typical_match() -> None:
    print("typical match:")
    cands = [
        hl("r03_multikill_00", 3.6, 2, 3),  # 2k with something extra
        hl("r07_ace_01", 9.5, 5, 7, kind="ace"),
        hl("r10_multikill_02", 5.5, 3, 10),
        hl("r12_multikill_03", 3.0, 2, 12),  # plain 2k
        hl("r13_multikill_06", 3.8, 2, 13),
        hl("r15_highlight_single_04", 1.5, 1, 15, kind="highlight_single"),
        hl("r18_clutch_05", 8.0, 3, 18, kind="clutch", clutch_vs=3),
    ]
    plan = run_rules_director(cands, EVENTS, MUSIC, "Tester")
    order = [d.clip_id for d in sorted(plan.decisions, key=lambda d: d.order)]
    by_id = {d.clip_id: d for d in plan.decisions}

    check("plan passes the shared validation",
          semantic_errors(plan, cands, MUSIC) == [])
    check("climax (ace) is last", order[-1] == "r07_ace_01", str(order))
    check("hook (second best) is first", order[0] == "r18_clutch_05", str(order))
    check("middle rises in score",
          [c for c in order[1:-1]] == ["r03_multikill_00", "r13_multikill_06",
                                        "r10_multikill_02"], str(order))
    check("plain 2k and weak single kill are dropped",
          plan.dropped_clip_ids == ["r12_multikill_03", "r15_highlight_single_04"],
          str(plan.dropped_clip_ids))
    check("ACE overlay", by_id["r07_ace_01"].overlay_text == "ACE")
    check("clutch overlay", by_id["r18_clutch_05"].overlay_text == "1v3 CLUTCH")
    check("3K overlay", by_id["r10_multikill_02"].overlay_text == "3K")
    check("no overlay on a 2k", by_id["r03_multikill_00"].overlay_text is None)

    climax_slow = by_id["r07_ace_01"].slowmo_segments
    check("climax gets slow-mo", len(climax_slow) == 1)
    if climax_slow:
        seg = climax_slow[0]
        check("slow-mo is around the last kill (7.0 s)",
              seg.start_s < 7.0 < seg.end_s, f"{seg.start_s}-{seg.end_s}")
    check("clutch gets slow-mo", len(by_id["r18_clutch_05"].slowmo_segments) == 1)
    check("small clips get no slow-mo",
          not by_id["r03_multikill_00"].slowmo_segments
          and not by_id["r10_multikill_02"].slowmo_segments)
    check("reasoning says it's the rules",
          all(d.reasoning.startswith("Rules:") for d in plan.decisions))
    check("intro is the player name", plan.intro_text == "TESTER")
    check("music is from the list", plan.music_track in MUSIC)
    again = run_rules_director(cands, EVENTS, MUSIC, "Tester")
    check("deterministic (same demo → same plan)",
          again.model_dump() == plan.model_dump())


def test_split_clutch() -> None:
    print("clutch split into two clips:")
    cands = [
        hl("r09_clutch_00", 6.0, 2, 9, start=1000, clutch_vs=3, kind="clutch"),
        hl("r09_clutch_01", 7.5, 2, 9, start=3000, clutch_vs=3, kind="clutch"),
        hl("r04_ace_02", 9.5, 5, 4, kind="ace"),
        hl("r12_multikill_03", 5.5, 3, 12),
    ]
    plan = run_rules_director(cands, EVENTS, MUSIC, "Tester")
    order = [d.clip_id for d in sorted(plan.decisions, key=lambda d: d.order)]
    by_id = {d.clip_id: d for d in plan.decisions}
    check("plan passes the shared validation",
          semantic_errors(plan, cands, MUSIC) == [])
    i0, i1 = order.index("r09_clutch_00"), order.index("r09_clutch_01")
    check("clutch parts back to back, chronological", i1 == i0 + 1, str(order))
    check("payoff part has the clutch overlay",
          by_id["r09_clutch_01"].overlay_text == "1v3 CLUTCH")
    check("first part has no clutch overlay",
          by_id["r09_clutch_00"].overlay_text is None)
    check("slow-mo only on the payoff part",
          not by_id["r09_clutch_00"].slowmo_segments
          and by_id["r09_clutch_01"].slowmo_segments)


def test_edge_cases() -> None:
    print("edge cases:")
    weak = [hl(f"r{i:02d}_multikill_{i:02d}", 2.0 + i * 0.1, 2, i) for i in range(5)]
    plan = run_rules_director(weak, EVENTS, MUSIC, "Tester")
    check("only weak clips → still MIN_CLIPS (3) clips", len(plan.decisions) == 3,
          f"{len(plan.decisions)}")
    check("the strongest weak clips are picked",
          {d.clip_id for d in plan.decisions}
          == {"r02_multikill_02", "r03_multikill_03", "r04_multikill_04"})

    many = [hl(f"r{i:02d}_multikill_{i:02d}", 4.0 + i * 0.1, 2, i) for i in range(12)]
    plan = run_rules_director(many, EVENTS, MUSIC, "Tester")
    check(f"many decent clips → capped at {MAX_CLIPS}",
          len(plan.decisions) == MAX_CLIPS, f"{len(plan.decisions)}")

    one = [hl("r01_multikill_00", 5.0, 2, 1)]
    plan = run_rules_director(one, EVENTS, MUSIC, "")
    check("a single clip works", len(plan.decisions) == 1)
    check("no player name → fallback intro", plan.intro_text == "FRAGSMITH")

    duel = [hl("r05_highlight_single_00", 2.0, 1, 5,
               kind="highlight_single", clutch_vs=1),
            hl("r08_ace_01", 9.5, 5, 8, kind="ace")]
    plan = run_rules_director(duel, EVENTS, MUSIC, "Tester")
    by_id = {d.clip_id: d for d in plan.decisions}
    check("won 1v1 duel gets slow-mo on the finish",
          len(by_id["r05_highlight_single_00"].slowmo_segments) == 1)

    short = [hl("r02_ace_00", 9.5, 5, 2, dur=7.2, kind="ace")]
    seg = run_rules_director(short, EVENTS, MUSIC, "T").decisions[0].slowmo_segments[0]
    check("slow-mo is clamped to the clip length", seg.end_s <= 7.2,
          f"end {seg.end_s}")


def test_no_anthropic_needed() -> None:
    print("works without anthropic:")
    check("importing the rules Director doesn't import anthropic",
          "anthropic" not in sys.modules)


if __name__ == "__main__":
    test_typical_match()
    test_split_clutch()
    test_edge_cases()
    test_no_anthropic_needed()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All rules Director tests OK")
