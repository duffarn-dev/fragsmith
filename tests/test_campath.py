"""Tests for lib/campath.py: the orbit camera for HLAE's mirv_campath.

Covers: look_at's angles (Source's convention), the orbit (radius, height,
starting behind the player, sweeping around, always aimed at the head,
following a moving player), and the XML (HLAE's format, game time, guard
keys, the yaw unwrapped across +-180).

Run: python tests/test_campath.py
"""
from __future__ import annotations

import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from lib.campath import (  # noqa: E402
    EYE_Z,
    ORBIT_HEIGHT,
    ORBIT_RADIUS,
    ORBIT_SWEEP,
    campath_xml,
    look_at,
    orbit_keys,
)

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def near(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol


def aim_error(k: dict, target: tuple[float, float, float]) -> float:
    """Degrees between where a key looks and the target."""
    p, y = math.radians(k["pitch"]), math.radians(k["yaw"])
    look = (math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), -math.sin(p))
    d = [t - c for t, c in zip(target, (k["x"], k["y"], k["z"]))]
    n = math.sqrt(sum(v * v for v in d))
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(a * b / n for a, b in zip(look, d))))))


def test_look_at() -> None:
    print("look_at")
    check("along +x: yaw 0, level", look_at((0, 0, 0), (10, 0, 0)) == (-0.0, 0.0)
          or look_at((0, 0, 0), (10, 0, 0)) == (0.0, 0.0))
    check("along +y: yaw 90", near(look_at((0, 0, 0), (0, 10, 0))[1], 90))
    check("down is positive pitch", near(look_at((0, 0, 10), (10, 0, 0))[0], 45))


def test_orbit() -> None:
    print("the orbit")
    still = [(100.0 + i * 0.5, 0.0, 0.0, 0.0) for i in range(5)]
    keys = orbit_keys(still, facing_yaw=0.0)
    head = (0.0, 0.0, EYE_Z)
    check("one key per sample, in game time", [k["t"] for k in keys] == [s[0] for s in still])
    check("at the radius", all(near(math.hypot(k["x"], k["y"]), ORBIT_RADIUS) for k in keys))
    check("above the eyes", all(near(k["z"], EYE_Z + ORBIT_HEIGHT) for k in keys))
    check("always aimed at the head", max(aim_error(k, head) for k in keys) < 0.1,
          f"{max(aim_error(k, head) for k in keys):.3f} deg off")
    # facing +x: behind is -x; the orbit is centered there
    mid = keys[len(keys) // 2]
    check("halfway, it's right behind the player", near(mid["x"], -ORBIT_RADIUS) and near(mid["y"], 0))
    first, last = (math.degrees(math.atan2(k["y"], k["x"])) % 360 for k in (keys[0], keys[-1]))
    check("it sweeps around them", near((last - first) % 360, ORBIT_SWEEP),
          f"{(last - first) % 360:.1f} deg")
    moving = [(100.0 + i * 0.5, i * 50.0, 0.0, 0.0) for i in range(5)]
    mkeys = orbit_keys(moving, facing_yaw=0.0)
    check("it follows a moving player",
          max(aim_error(k, (s[1], s[2], EYE_Z)) for k, s in zip(mkeys, moving)) < 0.1
          and all(near(math.hypot(k["x"] - s[1], k["y"] - s[2]), ORBIT_RADIUS)
                  for k, s in zip(mkeys, moving)))
    try:
        orbit_keys(still[:1], 0.0)
        check("one sample is refused", False)
    except ValueError:
        check("one sample is refused", True)


def test_xml() -> None:
    print("the XML")
    keys = orbit_keys([(1274.28125 + i * 0.25, 0.0, 0.0, 0.0) for i in range(9)], facing_yaw=170.0)
    xml = campath_xml(keys)
    check("HLAE's layout", xml.startswith('<?xml version="1.0" encoding="utf-8"?>\n<campath>\n<points>\n')
          and xml.rstrip().endswith("</points>\n</campath>"))
    pts = re.findall(r'<p t="([\d.]+)" x="[-\d.]+" y="[-\d.]+" z="[-\d.]+" fov="[\d.]+" '
                     r'rx="[-\d.]+" ry="[-\d.]+" rz="([-\d.]+)"/>', xml)
    check("every key has t x y z fov rx ry rz", len(pts) == 11, f"{len(pts)} points")
    ts = [float(t) for t, _ in pts]
    check("a guard key one tick outside each end",
          near(ts[0], keys[0]["t"] - 1 / 64, 1e-6) and near(ts[-1], keys[-1]["t"] + 1 / 64, 1e-6))
    check("game time, rising", ts == sorted(ts) and near(ts[1], 1274.28125, 1e-6))
    yaws = [float(y) for _, y in pts]
    check("the yaw never jumps across +-180 (no full spin)",
          max(abs(b - a) for a, b in zip(yaws, yaws[1:])) < 90,
          f"largest step {max(abs(b - a) for a, b in zip(yaws, yaws[1:])):.1f} deg")
    check("the unwrapped yaw passes 180 instead of flipping", max(yaws) > 180 or min(yaws) < -180)


if __name__ == "__main__":
    test_look_at()
    test_orbit()
    test_xml()
    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("All campath tests OK")
