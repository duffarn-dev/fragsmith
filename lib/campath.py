"""Camera paths for HLAE's mirv_campath, made from the demo's positions.

Stage 4 M6 (J): a camera that leaves the player's eyes and circles them at
the climax kill. The path is an XML file the clip's cfg loads (job schema
1.4): `mirv_campath load "<file>"` + `mirv_campath enabled 1`.

The format is what `mirv_campath save` writes: <campath><points> with one
<p t x y z fov rx ry rz/> per key. t is the demo's game time (demoparser2's
game_time, not the tick), rx is roll, ry pitch and rz yaw, in degrees. Known
from another CS2 tool tested with HLAE 2.192 (cs2cinema): the yaw must not
jump across +-180 (the spline would spin a full turn), and a copy of the end
keys just outside the path lets it ease in and out.
"""
from __future__ import annotations

import math

TICK_RATE = 64
EYE_Z = 56.0        # the player's origin is at the feet; look at the head
ORBIT_RADIUS = 140.0  # ~2.7 m: close enough to stay off most walls
ORBIT_HEIGHT = 30.0   # above the eyes, looking slightly down
ORBIT_SWEEP = 140.0   # degrees around the player over the whole path
ORBIT_FOV = 85.0


def look_at(cam: tuple[float, float, float], target: tuple[float, float, float]) -> tuple[float, float]:
    """(pitch, yaw) in Source's convention: yaw counter-clockwise from +x,
    pitch positive looking down."""
    dx, dy, dz = (t - c for t, c in zip(target, cam))
    yaw = math.degrees(math.atan2(dy, dx))
    pitch = -math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return pitch, yaw


def orbit_keys(
    samples: list[tuple[float, float, float, float]],
    facing_yaw: float,
    radius: float = ORBIT_RADIUS,
    height: float = ORBIT_HEIGHT,
    sweep: float = ORBIT_SWEEP,
    fov: float = ORBIT_FOV,
) -> list[dict]:
    """Keys circling the player. samples: (game_time, x, y, z) of the player
    at a few moments, in order. The camera starts behind them (opposite
    facing_yaw, their view at the first moment) minus half the sweep and
    swings around to the side, always looking at their head."""
    if len(samples) < 2:
        raise ValueError("an orbit needs the player at two moments or more")
    t0, t1 = samples[0][0], samples[-1][0]
    keys = []
    for t, x, y, z in samples:
        k = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
        a = math.radians(facing_yaw + 180.0 - sweep / 2 + sweep * k)
        target = (x, y, z + EYE_Z)
        cam = (x + radius * math.cos(a), y + radius * math.sin(a), z + EYE_Z + height)
        pitch, yaw = look_at(cam, target)
        keys.append({"t": t, "x": cam[0], "y": cam[1], "z": cam[2],
                     "pitch": pitch, "yaw": yaw, "roll": 0.0, "fov": fov})
    return keys


def campath_xml(keys: list[dict]) -> str:
    """The keys as mirv_campath XML, with a guard key one tick outside each
    end and the yaw unwrapped."""
    if not keys:
        raise ValueError("no keys")
    dt = 1.0 / TICK_RATE
    guarded = [{**keys[0], "t": keys[0]["t"] - dt}, *keys, {**keys[-1], "t": keys[-1]["t"] + dt}]
    lines = ['<?xml version="1.0" encoding="utf-8"?>', "<campath>", "<points>"]
    prev = None
    for k in guarded:
        yaw = k["yaw"]
        if prev is not None:
            yaw = prev + (yaw - prev + 180.0) % 360.0 - 180.0
        prev = yaw
        lines.append(
            f'<p t="{k["t"]:.6f}" x="{k["x"]:.2f}" y="{k["y"]:.2f}" z="{k["z"]:.2f}" '
            f'fov="{k["fov"]:.2f}" rx="{k["roll"]:.3f}" ry="{k["pitch"]:.3f}" rz="{yaw:.3f}"/>'
        )
    lines += ["</points>", "</campath>", ""]
    return "\n".join(lines)


def orbit_samples(demo_path: str, steamid: str, ticks: list[int]) -> tuple[list, float]:
    """The player's (game_time, x, y, z) at the ticks, and their view yaw at
    the first, from the demo."""
    from demoparser2 import DemoParser

    df = DemoParser(str(demo_path)).parse_ticks(["X", "Y", "Z", "yaw", "game_time"], ticks=ticks)
    df = df[df["steamid"].astype(str) == str(steamid)].sort_values("tick")
    if len(df) < 2:
        raise ValueError(f"player {steamid} isn't in the demo at those ticks")
    samples = [(float(r.game_time), float(r.X), float(r.Y), float(r.Z)) for r in df.itertuples()]
    return samples, float(df.iloc[0]["yaw"])
