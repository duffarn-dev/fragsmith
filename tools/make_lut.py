"""Genererer Fragsmith sin .cube-LUT (P5): subtil teal-orange filmlook.

Kjør på nytt for å justere looken (endre konstantene under):
    python tools/make_lut.py

Designet for å ligge OPPÅ eq/vibrance-graden i edit_engine (som allerede
løfter kontrast/metning) — derfor bevisst forsiktig:
- skygger dras mot teal (mindre rød, litt mer blå/grønn)
- høylys dras mot varmt/oransje (mer rød, mindre blå)
- svak S-kurve for filmisk kontrast, myk skulder så høylys ikke klipper
"""
from __future__ import annotations

from pathlib import Path

SIZE = 33               # standard oppløsning for .cube (33³ punkter)
TEAL_SHADOW = 0.040     # hvor mye skyggene dras mot teal
WARM_HIGHLIGHT = 0.035  # hvor mye høylysene dras mot oransje
S_CURVE = 0.10          # styrke på kontrast-S-kurven (0 = av)

OUT = Path(__file__).resolve().parent.parent / "assets" / "luts" / (
    "fragsmith-teal-orange.cube"
)


def _s_curve(x: float) -> float:
    """Svak S-kurve rundt 0.5 — smoothstep blandet inn med S_CURVE."""
    smooth = x * x * (3.0 - 2.0 * x)
    return x + S_CURVE * (smooth - x)


def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    ws = (1.0 - luma) ** 2   # skygge-vekt
    wh = luma ** 2           # høylys-vekt

    # split-toning: teal i skygger (B/G opp, R ned), varme i høylys
    r2 = r - TEAL_SHADOW * ws + WARM_HIGHLIGHT * wh
    g2 = g + 0.4 * TEAL_SHADOW * ws + 0.25 * WARM_HIGHLIGHT * wh
    b2 = b + TEAL_SHADOW * ws - WARM_HIGHLIGHT * wh

    out = tuple(_s_curve(min(1.0, max(0.0, c))) for c in (r2, g2, b2))
    return out  # type: ignore[return-value]


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "TITLE \"Fragsmith teal-orange (generert av tools/make_lut.py)\"",
        f"LUT_3D_SIZE {SIZE}",
        "DOMAIN_MIN 0.0 0.0 0.0",
        "DOMAIN_MAX 1.0 1.0 1.0",
    ]
    n = SIZE - 1
    # .cube-rekkefølge: rød varierer raskest, så grønn, så blå
    for bi in range(SIZE):
        for gi in range(SIZE):
            for ri in range(SIZE):
                r, g, b = transform(ri / n, gi / n, bi / n)
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    OUT.write_text("\n".join(lines) + "\n", encoding="ascii")
    print(f"Skrev {OUT} ({SIZE}^3 = {SIZE ** 3} punkter)")


if __name__ == "__main__":
    main()
